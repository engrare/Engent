"""Agent adapters. Both expose: await run(prompt) -> summary text, await close().

ClaudeEngine: structured channel, no terminal scraping. Every tool call arrives as typed JSON through the
Agent SDK: a PreToolUse hook enforces hard guardrails on *all* calls (even ones settings would auto-allow),
and can_use_tool turns the remaining decisions into phone approvals. The agent simply awaits the callback,
so a pending approval pauses nothing but that one tool call.

PtyEngine: fallback for arbitrary interactive CLIs (aider, ...). Runs them in a Windows ConPTY, drains output
on a thread (the child can never block on a full pipe), strips ANSI, and when output goes idle on a line that
looks like a prompt, forwards the screen to the phone and types the answer back."""
import asyncio
import collections
import difflib
import json
import logging
import os
import re
import threading
import time

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
                              PermissionResultAllow, PermissionResultDeny, RateLimitEvent, ResultMessage,
                              SystemMessage, TextBlock, ToolUseBlock)
from winpty import PtyProcess

from . import config as cfg, policy, store
from .tg import clip, esc

log = logging.getLogger("engent.engine")

SYSTEM_APPEND = """
You are running unattended as a background worker. Your working directory is an isolated git worktree on a
dedicated branch. The user is away from the desk and approves risky actions from their phone.
- Work autonomously. Never ask for confirmation in plain text; if a decision genuinely needs the user, use the
  AskUserQuestion tool.
- Stay inside the working directory. Never push, deploy, publish or change git config: the pipeline does that.
- Start servers/watchers only with run_in_background and stop them with TaskStop: process-kill commands are
  blocked, so anything started another way outlives you.
- Before each larger step, say in one short sentence what you are about to do: the user watches your latest
  sentence live on the phone to see that the work is moving.
- If the project has tests, run them before you finish.
- Commit your finished work on the current branch with a clear message.
- End with a 2-4 line summary of what changed, written in the language of the task."""

APPROVE, REJECT, ALL_EDITS = "✅ Onayla", "❌ Reddet", "✅ Bu görevde tüm düzenlemeler"
LIMITS = cfg.DATA / "limits.json"  # last plan-limit snapshot the CLI reported (5-hour / weekly utilization)


class EngineError(Exception):
    pass


# Models newer than the bundled CLI (2.1.281): the API serves them, the CLI just doesn't list them yet.
# "[1m]" = 1M context like the listed models (bare id gets 200k). Skipped once the CLI lists the model itself.
NEWER_MODELS = [{"value": "claude-sonnet-5-5[1m]", "displayName": "Sonnet 5.5", "description": "Sonnet 5.5 · Newest Sonnet",
                 "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]}]


async def list_models():
    """Models this account can use: the CLI's own /model list + NEWER_MODELS. Spawns the CLI, no API call."""
    client = ClaudeSDKClient(ClaudeAgentOptions(cwd=str(cfg.DATA), env=cfg.AGENT_ENV, setting_sources=[]))
    await client.connect()
    try:
        models = ((await client.get_server_info()) or {}).get("models") or []
    finally:
        await client.disconnect()
    known = {m.get("description", "").split(" · ")[0] for m in models}
    return models + [m for m in NEWER_MODELS if m["displayName"] not in known]


def _diff(tool, inp, cwd, rel):
    if tool == "Edit":
        pairs = [(inp.get("old_string", ""), inp.get("new_string", ""))]
    elif tool == "MultiEdit":
        pairs = [(e.get("old_string", ""), e.get("new_string", "")) for e in inp.get("edits", [])]
    elif tool == "Write":
        try:  # overwriting an existing file: show a real diff, not the whole file
            with open(os.path.join(cwd, inp["file_path"]), encoding="utf-8") as f:
                old = f.read()
        except (OSError, UnicodeDecodeError):
            old = ""
        pairs = [(old, inp.get("content", ""))]
    else:
        pairs = [("", inp.get("new_source", ""))]
    return "\n".join(line for old, new in pairs
                     for line in difflib.unified_diff(old.splitlines(), new.splitlines(), rel, rel, lineterm="", n=2))


def rel(path, cwd):
    try:
        return os.path.relpath(os.path.join(cwd, path), cwd)
    except ValueError:  # another drive
        return path


STEP_ICONS = {"Read": "📖", "Grep": "🔎", "Glob": "🔎", "WebFetch": "🌐", "WebSearch": "🌐", "AskUserQuestion": "❓",
              "Agent": "🤖", "Task": "🤖", **{t: "🖥" for t in policy.SHELLS}, **{t: "✏️" for t in policy.EDITS}}


def step(tool, inp, cwd):
    """One line per tool call for the live progress message: "🖥 Bash npm test", "✏️ Edit src/a.css"."""
    path = inp.get("file_path") or inp.get("notebook_path")
    what = rel(path, cwd) if path else (inp.get("description") or inp.get("command") or inp.get("pattern")
                                        or inp.get("url") or inp.get("query") or "")
    return f"{STEP_ICONS.get(tool, '🔧')} {tool} {' '.join(str(what).split())[:90]}".rstrip()


def describe(tool, inp, cwd, why=""):
    if tool in policy.SHELLS:
        body = f"🖥 <b>{tool}</b>\n<pre>{esc(clip(inp.get('command', ''), 1500))}</pre>"
        if inp.get("description"):
            body += f"\n<i>{esc(clip(inp['description'], 300))}</i>"
    elif tool in policy.EDITS:
        path = rel(inp.get("file_path") or inp.get("notebook_path") or "?", cwd)
        diff = _diff(tool, inp, cwd, path) or "(içerik değişmiyor)"
        body = f"✏️ <b>{tool}</b> <code>{esc(path)}</code>\n<pre><code class=\"language-diff\">{esc(clip(diff, 2800))}</code></pre>"
    else:
        body = f"🔧 <b>{esc(tool)}</b>\n<pre>{esc(clip(json.dumps(inp, ensure_ascii=False, indent=1), 1500))}</pre>"
    return body + (f"\n⚠️ {esc(why)}" if why else "")


class ClaudeEngine:
    def __init__(self, tid, cwd, proj, ask, write):
        self.tid, self.cwd, self.ask, self.write = tid, cwd, ask, write
        self.auto_edits = bool(proj.get("auto_edits"))
        self.extra_safe = proj.get("auto_allow", [])
        self.client = None
        self.abort = None
        # what the agent is doing, for the live progress message and /status
        self.said, self.steps, self.count, self.moved = "", collections.deque(maxlen=5), 0, time.time()

    async def run(self, prompt):
        if self.client is None:
            choice = cfg.model_choice()
            self.client = ClaudeSDKClient(ClaudeAgentOptions(
                cwd=self.cwd,
                resume=store.get(self.tid)["session_id"],  # continues the same conversation after a crash/follow-up
                model=choice.get("model"),
                effort=choice.get("effort"),
                permission_mode="default",
                setting_sources=["project"],  # project CLAUDE.md, but not the user's global allow-rules
                system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_APPEND},
                hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._guard])]},
                can_use_tool=self._approve,
                max_turns=cfg.MAX_TURNS,
                max_budget_usd=cfg.MAX_BUDGET_USD,
                env=cfg.AGENT_ENV,
                stderr=lambda line: self.write(f"[cli] {line}"),
                # SDK default is 1 MB per JSON line; a Read of a ~450 KB screenshot is already over it (base64,
                # sent twice in the tool result) and kills the whole task. Images are capped by the API well below this.
                max_buffer_size=64 * 1024 * 1024,
            ))
            await self.client.connect()
        self.abort = None
        await self.client.query(prompt)
        result = None
        async for m in self.client.receive_response():
            if isinstance(m, SystemMessage) and m.data.get("session_id"):
                store.update(self.tid, session_id=m.data["session_id"])
            elif isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, TextBlock):
                        self.write(b.text)
                        self.said, self.moved = b.text, time.time()
                    elif isinstance(b, ToolUseBlock):
                        self.write(f"⚙ {b.name} {json.dumps(b.input, ensure_ascii=False)[:400]}")
                        self.steps.append(step(b.name, b.input, self.cwd))
                        self.count, self.moved = self.count + 1, time.time()
            elif isinstance(m, RateLimitEvent):
                LIMITS.write_text(json.dumps(m.rate_limit_info.raw))
            elif isinstance(m, ResultMessage):
                result = m
        if self.abort:
            raise EngineError(self.abort)
        if result is None:
            raise EngineError("ajan sonuç dönmeden kapandı")
        # total_cost_usd is per session and survives resume, so it is already the task total
        store.update(self.tid, session_id=result.session_id, cost=result.total_cost_usd)
        try:
            await self.measure()
        except Exception as e:  # a missing gauge must not fail finished work
            log.warning("bağlam ölçülemedi: %r", e)
        self.write(f"— {result.subtype}: {result.num_turns} tur, ${result.total_cost_usd or 0:.2f}")
        if result.is_error or result.subtype != "success":
            raise EngineError(f"{result.subtype}: {'; '.join(result.errors or []) or result.result or ''}")
        return result.result or ""

    async def measure(self):
        """Current context fill into the store. The CLI answers instantly, even mid-turn or during a tool call."""
        if self.client:
            ctx = await asyncio.wait_for(self.client.get_context_usage(), 10)
            store.update(self.tid, ctx=ctx["totalTokens"], ctx_max=ctx["maxTokens"])

    async def _guard(self, data, tool_use_id, context):
        verdict, why = policy.check(data["tool_name"], data["tool_input"], self.cwd, self.extra_safe)
        if verdict != "deny":
            return {}
        self.write(f"⛔ {data['tool_name']}: {why}")
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": f"Blocked by Engent guardrail ({why}). No approval can unblock it, so do not "
                                        f"retry or ask for it; find another way or mention it in your summary."}}

    async def _approve(self, tool, inp, context):
        try:
            if tool == "AskUserQuestion":
                return PermissionResultAllow(updated_input={**inp, "answers": await self._questions(inp)})
            verdict, why = policy.check(tool, inp, self.cwd, self.extra_safe)
            if verdict == "deny":
                return PermissionResultDeny(message=f"Blocked by Engent guardrail: {why}")
            level = cfg.autonomy()
            if verdict == "allow" or level == "auto" or (verdict == "edit" and (self.auto_edits or level == "edits")):
                return PermissionResultAllow(updated_input=inp)
            options = [APPROVE, REJECT] + ([ALL_EDITS] if verdict == "edit" else [])
            answer = await self.ask(describe(tool, inp, self.cwd, why), options)
        except asyncio.TimeoutError:
            self.abort = "onay zaman aşımına uğradı"
            return PermissionResultDeny(message="The user did not respond in time.", interrupt=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let a notifier bug turn into an implicit "allow"
            log.exception("onay akışı hatası")
            self.abort = f"onay akışı hatası: {e}"
            return PermissionResultDeny(message="Approval channel failed.", interrupt=True)
        if answer == ALL_EDITS:
            self.auto_edits = True
        if answer in (APPROVE, ALL_EDITS):
            return PermissionResultAllow(updated_input=inp)
        if answer == REJECT:
            return PermissionResultDeny(message="The user rejected this action. Choose a different approach.")
        return PermissionResultDeny(message=f"The user rejected this action and replied: {answer}")

    async def _questions(self, inp):
        answers = {}
        for q in inp.get("questions", []):
            opts = q.get("options", [])
            body = f"❓ <b>{esc(q.get('header') or 'Soru')}</b>\n{esc(q['question'])}\n\n" + "\n".join(
                f"• <b>{esc(o['label'])}</b> — {esc(o.get('description', ''))}" for o in opts)
            if q.get("multiSelect"):
                body += "\n\n<i>Birden fazlası için yanıtla: “A, B”</i>"
            answers[q["question"]] = await self.ask(body, [o["label"] for o in opts])
        return answers

    async def close(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception as e:
                log.warning("disconnect: %s", e)
            self.client = None


ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
CURSOR_MOVE = re.compile(r"\x1b\[\d*(?:;\d*)?[Hf]")  # ConPTY repaints lines with cursor moves instead of \n
PROMPT_MARK = re.compile(r"\[[yn]/[yn]\]|\([yn]/[yn]\)|\([a-z]\)[a-z]|yes/no|\?", re.I)
CHOICE = re.compile(r"\(([A-Za-z])\)([A-Za-z' ]*)")  # aider: (Y)es/(N)o/(A)ll/(S)kip all
IDLE_SEC = 2.0
ENTER = "⏎ Enter"


def screen(raw):
    """Terminal text as a human would see it: no escapes, spinner redraws collapsed."""
    text = ANSI.sub("", CURSOR_MOVE.sub("\n", raw)).replace("\r\n", "\n")
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


def looks_like_prompt(line):
    return bool(line) and line[-1] in ":?])>" and bool(PROMPT_MARK.search(line))


class PtyEngine:
    def __init__(self, tid, cwd, proj, ask, write):
        self.cwd, self.ask, self.write = cwd, ask, write
        self.argv = proj["cmd"]
        self.proc = None

    async def run(self, prompt):
        argv = [a.replace("{task}", prompt) for a in self.argv]
        self.proc = proc = PtyProcess.spawn(argv, cwd=self.cwd, env={**os.environ, **cfg.AGENT_ENV}, dimensions=(50, 200))
        loop = asyncio.get_running_loop()
        chunks = asyncio.Queue()

        def pump():  # blocking reads live on a thread, so neither the child nor the event loop ever stalls
            try:
                while True:
                    loop.call_soon_threadsafe(chunks.put_nowait, proc.read(65536))
            except Exception:  # EOFError when the child exits
                pass
            finally:
                loop.call_soon_threadsafe(chunks.put_nowait, None)

        threading.Thread(target=pump, daemon=True).start()
        buf, asked = "", False
        while True:
            try:
                chunk = await asyncio.wait_for(chunks.get(), IDLE_SEC)
            except asyncio.TimeoutError:
                shown = screen(buf).rstrip()
                if not asked and looks_like_prompt(shown.rsplit("\n", 1)[-1].strip()) and proc.isalive():
                    asked = True  # ask once per screen; new output re-arms
                    proc.write(await self._prompt(shown) + "\r")
                continue
            if chunk is None:
                break
            buf = (buf + chunk)[-16384:]  # bounded: memory stays flat no matter how chatty the CLI is
            visible = ANSI.sub("", chunk).replace("\r\n", "\n").replace("\r", "\n")
            if visible.strip():
                asked = False
                self.write(visible)
        for _ in range(50):
            if not proc.isalive():
                break
            await asyncio.sleep(0.1)
        if proc.exitstatus not in (0, None):
            raise EngineError(f"çıkış kodu {proc.exitstatus}\n{screen(buf).strip()[-1500:]}")
        return screen(buf).strip()[-1500:]

    async def _prompt(self, shown):
        last = shown.rsplit("\n", 1)[-1]
        choices = {(key + rest).strip(): key.lower() for key, rest in CHOICE.findall(last)}
        if not choices and re.search(r"[yn]/[yn]", last, re.I):
            choices = {"Yes": "y", "No": "n"}
        tail = "\n".join(shown.splitlines()[-15:])
        answer = await self.ask(f"⌨️ <b>Terminal girdi bekliyor</b>\n<pre>{esc(clip(tail, 2500))}</pre>",
                                list(choices) + [ENTER])
        return "" if answer == ENTER else choices.get(answer, answer)

    async def close(self):
        if self.proc and self.proc.isalive():
            await asyncio.to_thread(self.proc.terminate, True)
