"""Task lifecycle (state machine, persisted in store):

  queued -> running <-> waiting -> testing -> (fixing -> testing)* -> deploying -> review -> merged | discarded
                                                                   no changes -> done
  any stage -> failed | cancelled | interrupted   (resumable with one tap; the agent session is resumed too)
  review/done/failed -> reply to the message on the phone = follow-up iteration on the same branch + session
"""
import asyncio
import ctypes
import json
import logging
import os
import re
import subprocess
import time

from . import config as cfg, store
from .engines import LIMITS, ClaudeEngine, PtyEngine
from .tg import clip, esc

log = logging.getLogger("engent.pipeline")

FIX_PROMPT = """The `{stage}` step failed after your changes. Output (tail):
```
{out}
```
Find the root cause and fix the code. Do not weaken, skip or delete tests to make them pass. If the failure is
not caused by the code (missing credentials, network, external service), change nothing and say why in one line."""
RESUME_PROMPT = ("The worker was interrupted while you were working. Check `git status` and `git diff`, "
                 "then finish the original task:\n\n{prompt}")
FOLLOWUP_PROMPT = "Follow-up request from the user on this same task and branch:\n\n{text}"

ICONS = {"queued": "⏳", "running": "⚙️", "waiting": "🔔", "testing": "🧪", "fixing": "🔧", "deploying": "🚀",
         "review": "👀", "merged": "🔀", "done": "✅", "discarded": "🗑", "failed": "❌", "cancelled": "⏹",
         "interrupted": "⚠️"}


class Failure(Exception):
    pass


def tail(s, n):
    return s[-n:]


def decode(b):
    """Console output: UTF-8, else the OEM code page Windows tools (PowerShell, cmd errors) write to a pipe.
    ponytail: whole-output choice; a mix of both keeps the UTF-8 part garbled."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode(f"cp{ctypes.windll.kernel32.GetOEMCP()}", "replace")


async def sh(cmd, cwd, env=None, timeout=None):
    """Shell command with a timeout; the whole process tree dies on timeout or cancel. -> (ok, output)"""
    # stdin=NUL: "pause" / prompts return at once instead of hanging until the timeout
    p = await asyncio.create_subprocess_shell(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout or cfg.CMD_TIMEOUT)
        return p.returncode == 0, decode(out)
    except asyncio.TimeoutError:
        return False, f"zaman aşımı: {cmd}"
    finally:
        if p.returncode is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)


async def git(cwd, *args, check=True):
    p = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await p.communicate()
    if p.returncode and check:
        raise Failure(f"git {' '.join(args)}: {err.decode('utf-8', 'replace').strip()}")
    return out.decode("utf-8", "replace").strip() if p.returncode == 0 else None


async def ident(cwd):
    """Commit identity fallback so a repo without user.email doesn't fail the whole task."""
    if await git(cwd, "config", "user.email", check=False):
        return []
    return ["-c", "user.name=Engent", "-c", "user.email=engent@localhost"]


def task_log(tid):
    path = cfg.DATA / "logs" / f"{tid}.log"

    def write(text):
        try:
            if path.exists() and path.stat().st_size > 20_000_000:
                return  # ponytail: hard cap per task; rotate if 20 MB ever proves too small
            with open(path, "a", encoding="utf-8") as f:
                f.write(text.rstrip() + "\n")
        except OSError:
            pass
    return write


def log_tail(tid, n=3000):
    path = cfg.DATA / "logs" / f"{tid}.log"
    return tail(path.read_text(encoding="utf-8", errors="replace"), n) if path.exists() else ""


def short(s, n):
    """One-line preview for lists (clip() is for blocks: it adds a line break)."""
    s = " ".join(s.split())
    return esc(s if len(s) <= n else s[: n - 1] + "…")


def doing(engine):
    """What a running agent is doing right now, one line: its last step and how long ago."""
    if not engine.count:
        return "başlıyor…"
    idle = (time.time() - engine.moved) / 60
    return esc(engine.steps[-1]) + (f" · <i>{idle:.0f} dk önce</i>" if idle >= 1 else "")


def status_text(live=None):
    """live: tid -> engine of the running Claude agents."""
    rows = store.recent()
    if not rows:
        return "Henüz görev yok."
    live = live or {}
    return "\n".join(f"{ICONS.get(r['state'], '•')} <b>#{r['id']}</b> {r['state']} · {esc(r['project'])} — "
                     f"{short(r['prompt'], 70)}" + (f"\n      ↳ {doing(live[r['id']])}" if r["id"] in live else "")
                     for r in rows) + ("\n\n<i>Canlı ilerleme: görevin ▶️ mesajı · /stop → durdur</i>" if live else "")


LIVE_EVERY = 15  # s between checks of the live progress message; it is edited only when its text changed


def live_text(tid, head, info, engine, t0, done=False):
    """The task's ▶️ message, rewritten in place (silent) while the agent works: state, time, last sentence, steps."""
    state = store.get(tid)["state"]
    top = f"🏁 {head} · ajan kapandı, sonuç aşağıda" if done else f"{ICONS.get(state, '•')} {head} · {state}"
    n = getattr(engine, "count", 0)  # the pty engine has no structured steps
    lines = [f"{top} · {(time.time() - t0) / 60:.0f} dk · {n} adım{info}"]
    if getattr(engine, "said", ""):
        lines.append(f"💬 <i>{short(engine.said, 300)}</i>")
    lines += [esc(x) for x in getattr(engine, "steps", ())]
    idle = (time.time() - engine.moved) / 60 if n else 0
    if not done and state == "running" and idle >= 2:
        lines.append(f"<i>⏳ {idle:.0f} dk'dır yeni adım yok: uzun bir komut çalışıyor ya da model düşünüyor</i>")
    return "\n".join(lines)


WINDOWS = {"five_hour": "5 saat", "seven_day": "haftalık"}
COMPACT_HINT = 50  # % context: suggest /compact above this (the CLI auto-compacts near the limit anyway)


def model_text():
    c = cfg.model_choice()
    return (c.get("label") or c.get("model") or "varsayılan") + (f" · efor {c['effort']}" if c.get("effort") else "")


def usage_line(t):
    """Context fill (what /compact shrinks) and the task's API-equivalent cost."""
    parts = []
    if t["ctx_max"]:
        parts.append(f"🧠 bağlam %{100 * t['ctx'] / t['ctx_max']:.0f} ({t['ctx'] // 1000}k/{t['ctx_max'] // 1000}k)")
    if t["cost"]:
        parts.append(f"≈${t['cost']:.2f}")
    return " · ".join(parts)


def limits_line():
    """Plan limits as last reported by the CLI; a window whose reset time has passed is shown as reset."""
    try:
        raw = json.loads(LIMITS.read_text())
    except (OSError, ValueError):
        return ""
    parts = ["⛔ limit doldu"] if raw.get("status") == "rejected" else []
    for key, w in raw.get("unifiedWindows", {}).items():
        label = WINDOWS.get(key, key)
        if (w.get("resetsAt") or 0) < time.time():
            parts.append(f"{label} sıfırlandı")
        else:
            reset = time.strftime("%d.%m %H:%M", time.localtime(w["resetsAt"]))
            parts.append(f"{label} %{(w.get('utilization') or 0) * 100:.0f} (↻ {reset})")
    return "📈 Plan: " + " · ".join(parts) if parts else ""


def usage_text(live=()):
    """live: ids of tasks with a running agent (their ctx was just re-measured)."""
    def row(r):
        return f"{ICONS.get(r['state'], '•')} <b>#{r['id']}</b> {usage_line(r) or 'başlıyor…'} — {short(r['prompt'], 40)}"
    finished = [r for r in store.recent() if r["id"] not in live and (r["ctx_max"] or r["cost"])]
    lines = [f"🤖 Model: {esc(model_text())} <i>(/model ile değiştir)</i>",
             limits_line() or "📈 Plan limiti henüz ölçülmedi; ilk görevden sonra görünür.", "", "<b>⚡ Çalışan ajanlar</b>"]
    lines += [row(store.get(t)) for t in sorted(live)] or ["Şu an çalışan ajan yok."]
    if finished:
        lines += ["", "<b>Son görevler</b>", *map(row, finished)]
    return "\n".join(lines + ["", "<i>/compact N → görevin konuşmasını özetleyip bağlamı küçültür</i>"])


class Worker:
    def __init__(self, tg):
        self.tg = tg
        self.jobs = {}
        self.engines = {}  # tid -> live ClaudeEngine, for on-demand context readings
        self.slots = asyncio.Semaphore(cfg.MAX_PARALLEL)

    def submit(self, tid, prompt=None):
        if tid in self.jobs:
            return False
        store.update(tid, state="queued")
        job = asyncio.create_task(self._run(tid, prompt))
        self.jobs[tid] = job
        job.add_done_callback(lambda _: self.jobs.pop(tid, None))
        return True

    def cancel(self, tid):
        job = self.jobs.get(tid)
        if job:
            job.cancel()
        return job is not None

    async def stop(self, tid):
        """/stop N and the ⏹ button. Not running: say what the task is instead, with its resume buttons."""
        t = store.get(tid)
        if not t:
            return await self.tg.send(f"#{tid} diye bir görev yok.", silent=True)
        if self.cancel(tid):
            return await self.tg.send(f"⏹ #{tid} durduruluyor…", silent=True)
        note = f"\n<i>{short(t['note'], 200)}</i>" if t["note"] and t["state"] == "failed" else ""
        await self.tg.send(f"#{tid} zaten çalışmıyor: {ICONS.get(t['state'], '•')} {t['state']}{note}",
                           self._retry(tid) if t["state"] in store.RESUMABLE else None, silent=True)

    async def recover(self):
        """After a crash/reboot: never-started tasks run; interrupted ones wait for one tap."""
        for t in store.in_states(store.ACTIVE):
            if t["state"] == "queued" and not t["worktree"]:
                self.submit(t["id"])
            else:
                store.update(t["id"], state="interrupted")
                await self._notify(t["id"], f"⚠️ <b>#{t['id']}</b> yeniden başlatmada kesildi.", self._retry(t["id"]))

    async def followup(self, tid, text):
        t = store.get(tid)
        if t["state"] not in store.FOLLOWUP or tid in self.jobs:
            return await self.tg.send(f"#{tid} şu an {t['state']}; ek istek alınamaz.", silent=True)
        self.submit(tid, FOLLOWUP_PROMPT.format(text=text))

    async def usage(self):
        """/usage: re-measure every running agent's context first, so the numbers are current, not end-of-turn."""
        live = dict(self.engines)
        for tid, engine in live.items():
            try:
                await engine.measure()
            except Exception as e:  # engine may be closing right now; show its last reading
                log.warning("#%s bağlam ölçülemedi: %r", tid, e)
        return usage_text(live)

    async def compact(self, tid):
        """Summarize the task's agent session in place, so later follow-ups start from a small context."""
        t = store.get(tid)
        if not t or tid in self.jobs or t["state"] not in store.FOLLOWUP or not t["session_id"] \
                or not (t["worktree"] and os.path.isdir(t["worktree"])):
            return await self.tg.send(f"#{tid} sıkıştırılamaz: çalışıyor, kapanmış ya da Claude oturumu yok.", silent=True)
        proj = cfg.projects().get(t["project"], {})
        engine = ClaudeEngine(tid, t["worktree"], proj, self._asker(tid, f"<b>#{tid}</b>"), task_log(tid))
        self.jobs[tid] = asyncio.current_task()  # blocks follow-ups/resume on the same session meanwhile
        await self.tg.send(f"🗜 #{tid} sıkıştırılıyor… önce: {usage_line(t) or '?'}", silent=True)
        try:
            await engine.run("/compact")
        except Exception as e:
            return await self.tg.send(f"⚠️ #{tid} sıkıştırılamadı: {esc(clip(str(e), 500))}", silent=True)
        finally:
            self.jobs.pop(tid, None)
            await engine.close()
        await self.tg.send(f"🗜 #{tid} sıkıştırıldı · {usage_line(store.get(tid))}", silent=True)

    async def action(self, act, tid):
        t = store.get(tid)
        if not t:
            return
        if act == "r":
            if t["state"] in store.RESUMABLE and tid not in self.jobs:
                self.submit(tid, RESUME_PROMPT.format(prompt=t["prompt"]))
            else:
                await self.tg.send(f"#{tid} şu an {t['state']}; devam ettirilemez.", silent=True)
        elif act == "s":
            await self.stop(tid)
        elif act == "m":
            await self.tg.send(await self._merge(t) if t["state"] == "review" else f"#{tid} merge için hazır değil.")
        elif act == "d":
            store.update(tid, state="discarded")  # first, so the cancelled job stays quiet
            job = self.jobs.get(tid)
            if job:
                job.cancel()
                await asyncio.wait([job], timeout=30)
            await self._cleanup(t)
            await self.tg.send(f"🗑 #{tid} silindi (worktree + branch).", silent=True)

    # --- internals -------------------------------------------------------------------------------------------

    def _retry(self, tid):
        return [("▶️ Devam et", f"t:r:{tid}"), ("🗑 Sil", f"t:d:{tid}")]

    async def _notify(self, tid, text, buttons=None):
        """Loud message tied to a task: replying to it later becomes a follow-up for that task."""
        msg = await self.tg.send(text, buttons)
        store.update(tid, msg_id=msg["message_id"])

    def _asker(self, tid, head):
        async def ask(body, options):
            prev = store.get(tid)["state"]
            store.update(tid, state="waiting")
            try:
                return await self.tg.ask(f"{head}\n{body}", options, cfg.APPROVAL_TIMEOUT)
            finally:
                store.update(tid, state="running" if prev == "waiting" else prev)  # parallel asks
        return ask

    async def _run(self, tid, prompt):
        head = f"<b>#{tid}</b> · {esc(store.get(tid)['project'])}"
        async with self.slots:
            try:
                await asyncio.wait_for(self._flow(tid, head, prompt), cfg.TASK_TIMEOUT)
            except asyncio.CancelledError:
                if store.get(tid)["state"] != "discarded":
                    store.update(tid, state="cancelled")
                    await self._notify(tid, f"⏹ {head} durduruldu.", self._retry(tid))
                raise
            except Exception as e:
                if not isinstance(e, (Failure, asyncio.TimeoutError)):
                    log.exception("görev #%s", tid)
                reason = "zaman aşımı" if isinstance(e, asyncio.TimeoutError) else str(e) or type(e).__name__
                store.update(tid, state="failed", note=reason[:1000])
                await self._notify(tid, f"❌ {head} başarısız\n<pre>{esc(clip(reason, 1500))}</pre>", self._retry(tid))

    async def _flow(self, tid, head, prompt):
        t = store.get(tid)
        proj = cfg.projects().get(t["project"])
        if not proj:
            raise Failure(f"projects.json'da '{t['project']}' yok")
        write = task_log(tid)
        wt = t["worktree"]
        if not wt or not os.path.isdir(wt):
            wt = await self._worktree(tid, t, proj, write)
            prompt = t["prompt"]  # fresh checkout: old session edits are gone, start clean
            store.update(tid, session_id=None)
        store.update(tid, state="running")
        engine_cls = PtyEngine if proj.get("engine") == "pty" else ClaudeEngine
        info = f" · <code>engent/{tid}</code>" + (f" · 🤖 {esc(model_text())}" if engine_cls is ClaudeEngine else "")
        stop = [("⏹ Durdur", f"t:s:{tid}")]
        mid = (await self.tg.send(f"▶️ {head} çalışıyor{info}", stop, silent=True))["message_id"]
        write(f"\n===== {prompt or t['prompt']}")
        engine = engine_cls(tid, wt, proj, self._asker(tid, head), write)
        if engine_cls is ClaudeEngine:
            self.engines[tid] = engine
        t0 = time.time()
        live = asyncio.create_task(self._live(mid, lambda: live_text(tid, head, info, engine, t0), stop))
        try:
            summary = await engine.run(prompt or t["prompt"])
            for attempt in range(cfg.MAX_FIX_ATTEMPTS + 1):
                failed = await self._verify(tid, t, proj, wt, write)
                if not failed:
                    break
                stage, out = failed
                if attempt == cfg.MAX_FIX_ATTEMPTS:
                    raise Failure(f"{stage} {attempt} düzeltme denemesinden sonra hâlâ başarısız:\n{tail(out, 1200)}")
                store.update(tid, state="fixing")
                await self.tg.send(f"🔧 {head} {stage} kırmızı → ajan düzeltiyor ({attempt + 1}/{cfg.MAX_FIX_ATTEMPTS})",
                                   silent=True)
                summary = await engine.run(FIX_PROMPT.format(stage=stage, out=tail(out, 6000)))
        finally:
            live.cancel()
            self.engines.pop(tid, None)
            await engine.close()
            await self.tg.edit(mid, live_text(tid, head, info, engine, t0, done=True))  # no buttons: nothing to stop
        await self._report(tid, head, proj, wt, summary)

    async def _live(self, mid, render, buttons):
        shown = None
        while True:
            await asyncio.sleep(LIVE_EVERY)
            text = render()
            if text != shown:
                shown = text
                await self.tg.edit(mid, text, buttons)

    async def _worktree(self, tid, t, proj, write):
        repo, branch = proj["path"], f"engent/{tid}"
        wt = str(cfg.WORKTREES / f"{t['project']}-{tid}")
        cfg.WORKTREES.mkdir(parents=True, exist_ok=True)
        await git(repo, "worktree", "prune", check=False)
        if await git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False) is not None:
            await git(repo, "worktree", "add", wt, branch)  # keep existing commits on the branch
        else:
            await git(repo, "worktree", "add", "-b", branch, wt, proj["base"])
        store.update(tid, worktree=wt, branch=branch)
        if proj.get("setup"):
            ok, out = await sh(proj["setup"], wt)
            write(f"$ {proj['setup']}\n{out}")
            if not ok:
                raise Failure(f"setup başarısız:\n{tail(out, 1500)}")
        return wt

    async def _verify(self, tid, t, proj, wt, write):
        """test -> commit -> deploy. Returns None on success, else (stage, output) to feed back to the agent."""
        if proj.get("test"):
            store.update(tid, state="testing")
            ok, out = await sh(proj["test"], wt)
            write(f"$ {proj['test']}\n{out}")
            if not ok:
                return "test", out
        if await git(wt, "status", "--porcelain"):
            await git(wt, "add", "-A")
            await git(wt, *await ident(wt), "commit", "-m", f"engent #{tid}: {t['prompt'][:60]}")
        if not proj.get("deploy") or not int(await git(wt, "rev-list", "--count", f"{proj['base']}..HEAD")):
            return None
        store.update(tid, state="deploying")
        env = {**os.environ, **{k: cfg.ENV[k] for k in proj.get("deploy_env", []) if k in cfg.ENV}}
        ok, out = await sh(proj["deploy"], wt, env=env)
        write(f"$ {proj['deploy']}\n{out}")
        if not ok:
            return "deploy", out
        urls = re.findall(proj.get("url_regex", r"https://[^\s\"'<>]+"), out)
        store.update(tid, url=urls[-1] if urls else None)
        return None

    async def _report(self, tid, head, proj, wt, summary):
        base = proj["base"]
        if not int(await git(wt, "rev-list", "--count", f"{base}..HEAD")):
            store.update(tid, state="done")
            return await self._notify(tid, f"✅ {head} bitti — kod değişikliği yok.\n\n{esc(clip(summary, 1500))}",
                                      [("🗑 Temizle", f"t:d:{tid}")])
        stat = await git(wt, "diff", "--shortstat", f"{base}...HEAD")
        url = store.get(tid)["url"]
        store.update(tid, state="review")
        lines = [f"✅ {head} hazır" + (" — testler geçti" if proj.get("test") else ""), "",
                 esc(clip(summary, 1500)), "", f"📊 {esc(stat)}"]
        if url:
            lines.append(f"🔗 {esc(url)}")
        t = store.get(tid)
        lines += [x for x in (usage_line(t), limits_line()) if x]
        if t["ctx_max"] and 100 * t["ctx"] / t["ctx_max"] >= COMPACT_HINT:
            lines.append(f"💡 Bağlam dolu; ek istekten önce /compact {tid}")
        lines.append("<i>↩️ Yanıtlayarak ek istek gönderebilirsin.</i>")
        await self._notify(tid, "\n".join(lines), [(f"🔀 {base} dalına merge", f"t:m:{tid}"), ("🗑 Sil", f"t:d:{tid}")])

    async def _merge(self, t):
        proj = cfg.projects()[t["project"]]
        repo, base = proj["path"], proj["base"]
        current = await git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if current != base:
            return f"⚠️ Ana repo '{current}' dalında; merge için '{base}' checkout'lu olmalı."
        if await git(repo, "status", "--porcelain", "--untracked-files=no"):
            return "⚠️ Ana repoda commit'lenmemiş değişiklik var; merge'ü masada yap."
        msg = f"engent #{t['id']}: {t['prompt'][:60]}"
        if await git(repo, *await ident(repo), "merge", "--no-ff", "-m", msg, t["branch"], check=False) is None:
            await git(repo, "merge", "--abort", check=False)
            return f"⚠️ #{t['id']} {base} ile çakışıyor; merge geri alındı. Yanıtlayıp ajandan rebase isteyebilirsin."
        if proj.get("push_on_merge"):
            await git(repo, "push", "origin", base)
        await self._cleanup(t)
        store.update(t["id"], state="merged")
        return f"🔀 #{t['id']} → {base} merge edildi" + (" ve push'landı." if proj.get("push_on_merge") else ".")

    async def _cleanup(self, t):
        repo = cfg.projects().get(t["project"], {}).get("path")
        if not repo:
            return
        if t["worktree"]:
            await git(repo, "worktree", "remove", "--force", t["worktree"], check=False)
        await git(repo, "worktree", "prune", check=False)
        if t["branch"]:
            await git(repo, "branch", "-D", t["branch"], check=False)
