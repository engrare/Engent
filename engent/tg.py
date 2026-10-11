"""Telegram Bot API over outbound HTTPS long polling: no open ports, no tunnel, NAT-proof.
ask() is the human-in-the-loop primitive: send buttons, then await a tap or a text reply."""
import asyncio
import html
import logging
import re
import secrets

import httpx

from . import config as cfg

log = logging.getLogger("engent.tg")


def esc(s):
    return html.escape(str(s), quote=False)


def clip(s, n=3000):
    """Clip raw text (before esc) so messages stay under Telegram's 4096-char limit."""
    s = str(s)
    return s if len(s) <= n else s[: n - 30] + f"\n… (+{len(s) - n + 30} karakter)"


def keyboard(buttons):
    return {"inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in buttons]}


class Telegram:
    def __init__(self, token, users):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat = users[0]  # private chat id == user id
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(75, connect=20))
        self.pending = {}  # nonce -> (future, options)
        self.replies = {}  # message_id -> nonce
        self._bg = set()

    async def call(self, method, *, force=False, **params):
        """Retries network errors, 5xx and 429 forever (an approval must survive a Wi-Fi drop); raises on 4xx.
        Lockout: only polling goes out (updates are read and dropped); force = the last message before it."""
        if cfg.locked_out() and not force and method != "getUpdates":
            return {"message_id": 0}  # callers that keep the id of a sent message still get one
        delay = 1
        while True:
            try:
                r = await self.http.post(self.base + method, json=params)
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("%s: %r", method, e)  # %r: httpx timeouts have an empty str()
            else:
                if data.get("ok"):
                    return data["result"]
                retry = data.get("parameters", {}).get("retry_after")
                if not retry and r.status_code < 500:
                    raise RuntimeError(f"{method}: {data.get('description')}")
                delay = retry or delay
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def send(self, text, buttons=None, silent=False, force=False):
        """silent=True: arrives without sound/vibration. Only decisions and results buzz the phone."""
        params = {"chat_id": self.chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True, "disable_notification": silent}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        try:
            return await self.call("sendMessage", force=force, **params)
        except RuntimeError as e:  # bad HTML / too long: degrade to plain text instead of losing the message
            log.warning("HTML reddedildi, düz metne düşülüyor: %s", e)
            params.pop("parse_mode")
            params["text"] = html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]
            return await self.call("sendMessage", force=force, **params)

    async def ask(self, text, options, timeout):
        """Returns the tapped option label, or the free text the user replied with."""
        if cfg.locked_out():  # nobody can answer: fail closed now instead of holding the task for `timeout`
            raise asyncio.TimeoutError
        nonce = secrets.token_hex(4)
        fut = asyncio.get_running_loop().create_future()
        self.pending[nonce] = (fut, options)
        msg = await self.send(text + "\n\n<i>↩️ Yanıtlayarak serbest metin yazabilirsin.</i>",
                              [(o, f"a:{nonce}:{i}") for i, o in enumerate(options)])
        self.replies[msg["message_id"]] = nonce
        outcome = "⏱ zaman aşımı"
        try:
            outcome = await asyncio.wait_for(fut, timeout)
            return outcome
        except asyncio.CancelledError:
            outcome = "⏹ iptal"
            raise
        finally:
            self.pending.pop(nonce, None)
            self.replies.pop(msg["message_id"], None)
            # Fire-and-forget: a slow network must not hold up cancellation.
            job = asyncio.create_task(self._settle(msg["message_id"], text, outcome))
            self._bg.add(job)
            job.add_done_callback(self._bg.discard)

    async def _settle(self, message_id, text, outcome):
        """Replace the buttons with the decision so a stale message can't be tapped twice."""
        await self.edit(message_id, f"{text}\n\n→ <b>{esc(clip(outcome, 300))}</b>")

    async def edit(self, message_id, text, buttons=None):
        """Rewrite a message in place (menus, settled asks). No buttons = keyboard removed."""
        params = {"reply_markup": keyboard(buttons)} if buttons else {}
        try:
            await self.call("editMessageText", chat_id=self.chat, message_id=message_id, text=text, parse_mode="HTML",
                            disable_web_page_preview=True, **params)
        except RuntimeError as e:  # e.g. "message is not modified"
            log.warning("mesaj güncellenemedi: %s", e)

    def on_callback(self, data):
        _, nonce, i = data.split(":")
        fut, options = self.pending.get(nonce, (None, None))
        if fut is None or fut.done():
            return False
        fut.set_result(options[int(i)])
        return True

    def on_reply(self, message_id, text):
        fut = self.pending.get(self.replies.get(message_id), (None,))[0]
        if fut is None or fut.done():
            return False
        fut.set_result(text)
        return True

    async def updates(self):
        offset = 0
        while True:
            try:
                batch = await self.call("getUpdates", offset=offset, timeout=50,
                                        allowed_updates=["message", "callback_query"])
            except Exception as e:  # e.g. 409: another instance is polling
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(10)
                continue
            for u in batch:
                offset = u["update_id"] + 1
                yield u
