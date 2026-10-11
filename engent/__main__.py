"""python -m engent  — worker daemon: polls Telegram, runs tasks, never exits on its own."""
import asyncio
import hmac
import logging
import logging.handlers
import msvcrt
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

from . import config as cfg, policy, store
from .engines import list_models
from .pipeline import Failure, Worker, git, ident, log_tail, model_text, sh, status_text, tail
from .tg import Telegram, clip, esc

log = logging.getLogger("engent")

# Single source for /help and Telegram's "/" menu. Only tasks (and /compact) run Claude; commands cost no tokens.
COMMANDS = [
    ("new", "proje görev", "Belirli projede yeni görev"),
    ("status", "", "Son görevler; çalışan ajanın şu an ne yaptığı"),
    ("log", "N", "Görev N'in son log satırları"),
    ("stop", "[N]", "Görev N'i durdur (N'siz: çalışanlar butonla)"),
    ("usage", "", "Plan limiti + çalışan ajanların anlık bağlamı + görev maliyetleri"),
    ("model", "", "Ajan modelini ve efor seviyesini butonla seç (yeni görevlerde geçerli)"),
    ("izin", "", "Ajan ne zaman onay sorsun: her şeyi / sadece komutları / hiçbir şeyi"),
    ("compact", "N", "Görev N'in bağlamını özetleyip küçült (az token harcar)"),
    ("projects", "", "Projeler (⭐ = varsayılan)"),
    ("use", "ad", "Varsayılan projeyi değiştir"),
    ("add", "ad [yol]", "Klasörü proje ekle (yol yoksa bulunduğun klasör); git değilse init + ilk commit"),
    ("ls", "[yol]", "Klasörü listele; butonlarla gez, ⭐ ile o klasörle çalış"),
    ("cd", "yol", "Klasöre geç (.. = üst, D: = sürücü, boş = ev klasörü)"),
    ("run", "komut", "Bulunduğun klasörde exe/cmd/bat/ps1 ya da komut çalıştır (onay butonu, çıktı gelir, pencere açılmaz)"),
    ("help", "", "Bu liste"),
]
HELP = "\n".join([
    "<b>Engent</b> — telefondan kodlama ajanı. Komutlar token harcamaz; yalnızca görevler Claude'u çalıştırır.",
    "",
    "• Düz mesaj → ⭐ varsayılan projede <b>yeni</b> görev (son 24 saatte açık görev varsa önce devam mı diye sorar)",
    "• Bir görevin mesajını ↩️ <b>yanıtla</b> → aynı göreve ek istek",
    "• Onay/soru mesajını ↩️ yanıtla → butonsuz serbest cevap",
    "• Çalışan görevin ▶️ mesajı canlı güncellenir: ajanın son cümlesi, son adımları, ⏹ Durdur butonu",
    "",
    *(f"/{c}{' <code>' + a + '</code>' if a else ''} — {d}" for c, a, d in COMMANDS),
])
MENU = [{"command": c, "description": d[:256]} for c, _, d in COMMANDS]
GITIGNORE ="node_modules/\n.venv/\nvenv/\n__pycache__/\ndist/\nbuild/\n.next/\n.env\n.env.*\n!.env.example\n*.log\n"
MAX_DIRS = 30
HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
MARKS = (("🟢 git", ".git"), ("📦 package.json", "package.json"), ("🐍 pyproject", "pyproject.toml"),
         ("🐍 requirements", "requirements.txt"))
EFFORT_HINT = {"low": "en hızlı", "medium": "dengeli", "high": "derin (varsayılan)", "xhigh": "daha derin",
               "max": "en derin, limiti en hızlı tüketir"}
AUTONOMY = [("ask", "🔒 Her şeyi sor", "her dosya düzenlemesi ve tanınmayan her komut telefondan onay bekler"),
            ("edits", "✏️ Düzenlemeler otomatik", "worktree'deki dosya düzenlemeleri sormadan; komutlar onayla"),
            ("auto", "🚀 Tam otonom", "hiçbir şey sormaz (npm install, sunucu açma, internet dahil)")]
model_menus = {}  # message_id -> {"models": [...], "pick": model}
nav = {"cwd": str(Path.home())}  # folder browser position (single user)
listings = {}  # message_id -> (folder, subfolders): each listing's buttons keep pointing at their own folder
runs = {}  # message_id -> (command, folder): a /run waiting for its confirming tap
routes = {}  # message_id -> (project, text): a plain message waiting for "new task or continue #N?"


def resolve(path):
    path = path.strip().strip("\"'")
    if re.fullmatch(r"[a-zA-Z]:", path):
        path += "\\"  # "D:" alone means D's cwd on Windows; the user means the drive root
    return os.path.abspath(os.path.join(nav["cwd"], os.path.expanduser(path or "~")))


def listing(folder):
    """Names only, never file contents. Hidden/system entries (AppData, junctions, .git) are skipped."""
    dirs, files = [], []
    for e in os.scandir(folder):
        try:
            if e.name.startswith((".", "$")) or e.stat().st_file_attributes & HIDDEN:
                continue
            (dirs if e.is_dir() else files).append(e.name)
        except OSError:
            continue
    dirs.sort(key=str.lower)
    files.sort(key=str.lower)
    marks = [m for m, f in MARKS if os.path.exists(os.path.join(folder, f))]
    marks += [f"⭐ proje: {project_at(folder)}"] if project_at(folder) else []
    lines = [f"📂 <code>{esc(folder)}</code>"] + ([" · ".join(esc(m) for m in marks)] if marks else [])
    lines.append(f"📁 {len(dirs)} klasör · 📄 {len(files)} dosya")
    if files:
        lines.append(esc(clip(", ".join(files[:15]) + (" …" if len(files) > 15 else ""), 800)))
    if len(dirs) > MAX_DIRS:
        lines.append(f"<i>İlk {MAX_DIRS} klasör gösteriliyor; diğerleri için /cd ad</i>")
    up = os.path.dirname(folder)
    buttons = ([("⬆️ Üst klasör", "f:u")] if up != folder else [])
    buttons += [(f"📁 {d}", f"f:{i}") for i, d in enumerate(dirs[:MAX_DIRS])]
    buttons.append(("⭐ Bu klasörle çalış", "f:p"))
    return "\n".join(lines), buttons, dirs


def same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


async def show(tg, folder, message_id=None):
    """New listing message, or (button tap) the same message edited in place so the chat doesn't fill up."""
    try:
        text, buttons, dirs = listing(folder)
    except OSError as e:
        return await tg.send(f"⚠️ {esc(e.strerror or e)}: <code>{esc(folder)}</code>", silent=True)
    nav["cwd"] = folder
    if message_id:
        await tg.edit(message_id, text, buttons)
    else:
        message_id = (await tg.send(text, buttons, silent=True))["message_id"]
    listings[message_id] = (folder, dirs)


def model_label(m):
    name = m.get("description", "").split(" · ")[0] or m.get("displayName") or m["value"]
    return f"Varsayılan ({name})" if m["value"] == "default" else name


async def model_menu(tg):
    """/model step 1: the account's models as buttons (list comes from the CLI, so it is never stale)."""
    try:
        models = await asyncio.wait_for(list_models(), 60)
    except Exception as e:
        return await tg.send(f"⚠️ Model listesi alınamadı: {esc(clip(repr(e), 300))}", silent=True)
    lines = [f"🤖 Şu an: <b>{esc(model_text())}</b>", ""]
    lines += [f"• <b>{esc(model_label(m))}</b> — {esc(m.get('description', '').partition(' · ')[2])}" for m in models]
    buttons = [(model_label(m) + (" 💳" if "credit" in m.get("description", "").lower() else ""), f"m:{i}")
               for i, m in enumerate(models)]  # 💳 = billed from extra usage credits, not the plan
    mid = (await tg.send("\n".join(lines + ["", "Model seç:"]), buttons, silent=True))["message_id"]
    model_menus[mid] = {"models": models, "pick": None}


async def tap_model(tg, mid, data):
    """Step 2: effort buttons for the tapped model; step 3: save. Running agents keep their model."""
    menu = model_menus[mid]
    kind, _, val = data.partition(":")
    if kind == "m":
        menu["pick"] = m = menu["models"][int(val)]
        levels = m.get("supportedEffortLevels") or []
        if levels:
            return await tg.edit(mid, f"🤖 <b>{esc(model_label(m))}</b>\nEfor seviyesi seç (ne kadar derin düşünsün):",
                                 [(f"{lv} — {EFFORT_HINT.get(lv, '')}", f"e:{lv}") for lv in levels])
        val = ""  # model without effort support (Haiku)
    m = menu["pick"]
    cfg.save_model({"model": None if m["value"] == "default" else m["value"], "effort": val or None,
                    "label": model_label(m)})
    model_menus.pop(mid)
    await tg.edit(mid, f"✅ Model: <b>{esc(model_text())}</b>\nYeni görevlerde geçerli; çalışan ajanlar mevcut "
                       f"modelleriyle devam eder.")


def autonomy_menu():
    """/izin: stateless buttons (p:level), so an old menu message still works after a restart."""
    now = cfg.autonomy()
    lines = ["🛡 <b>Ajan ne zaman onay sorsun?</b>", ""]
    lines += [f"{'👉' if k == now else '•'} <b>{label}</b> — {esc(d)}" for k, label, d in AUTONOMY]
    lines += ["", "<i>Her seviyede: okuma/test komutları sormadan çalışır; guardrail yasakları (push, deploy, "
              "sistem ayarı, sır dosyaları, worktree dışına yazma) hiç çalışmaz; merge her zaman senin butonunla. "
              "Değişiklik çalışan ajanların sonraki adımlarına da hemen uygulanır.</i>"]
    return "\n".join(lines), [(label + (" ✓" if k == now else ""), f"p:{k}") for k, label, _ in AUTONOMY]


async def confirm_run(tg, command):
    """/run step 1: show exactly what runs where; nothing starts without the tap."""
    first = re.match(r'"([^"]+)"|\S+', command)
    path = os.path.join(nav["cwd"], first.group(1) or first.group(0))
    if os.path.isfile(path):  # full path: cmd skips the current dir when NoDefaultCurrentDirectoryInExePath is set
        command = f'"{path}"{command[first.end():]}'
        if path.lower().endswith(".ps1"):  # cmd.exe would open a .ps1 in Notepad
            command = f"powershell -NoProfile -ExecutionPolicy Bypass -File {command}"
    mid = (await tg.send(f"▶️ Çalıştırılsın mı?\n📂 <code>{esc(nav['cwd'])}</code>\n<pre>{esc(clip(command, 1500))}</pre>",
                         [("▶️ Çalıştır", "x:go")], silent=True))["message_id"]
    runs[mid] = (command, nav["cwd"])


async def run_command(tg, mid):
    """Step 2: run with stdin=NUL and CMD_TIMEOUT (whole tree killed), then send the output's tail."""
    job = runs.pop(mid, None)
    if not job:  # double tap
        return
    command, folder = job
    await tg.edit(mid, f"⏳ Çalışıyor…\n📂 <code>{esc(folder)}</code>\n<pre>{esc(clip(command, 1500))}</pre>")
    start = time.monotonic()
    ok, out = await sh(command, folder)
    await tg.send(f"{'✅' if ok else '❌'} <code>{esc(clip(command, 200))}</code> · {time.monotonic() - start:.0f} sn\n"
                  f"<pre>{esc(tail(out.strip(), 3000)) or '(çıktı yok)'}</pre>")


def project_at(folder):
    return next((n for n, p in cfg.projects().items() if same_path(p["path"], folder)), None)


async def tap_folder(tg, message_id, key):
    folder, dirs = listings[message_id]
    if key == "p" and not project_at(folder) and not os.path.exists(os.path.join(folder, ".git")):
        # one tap must not git-init a wrong folder (Downloads, Documents...): confirm first
        mid = (await tg.send(f"⚠️ <code>{esc(folder)}</code> git reposu değil.\n.gitignore + git init + ilk commit "
                             f"yapılıp proje olarak eklensin mi?", [("✅ Evet, proje yap", "f:P")], silent=True))
        listings[mid["message_id"]] = (folder, [])
    elif key in ("p", "P"):
        await tg.send(await pick_folder(folder), silent=True)
    else:
        await show(tg, os.path.dirname(folder) if key == "u" else os.path.join(folder, dirs[int(key)]), message_id)


async def pick_folder(folder):
    """⭐ button: make this folder the default project, registering it first if needed."""
    name = project_at(folder)
    if name:
        return use_project(name)
    name = re.sub(r"[^\w.-]+", "-", os.path.basename(folder.rstrip("\\/"))).strip("-").lower() or "proje"
    out = await add_project(name, folder)
    return out + "\n" + use_project(name) if same_path(cfg.projects().get(name, {}).get("path", ""), folder) else out


async def new_task(tg, worker, project, prompt):
    projects = cfg.projects()
    if project not in projects:
        return await tg.send(f"Proje yok: {esc(project)}. Mevcut: {esc(', '.join(projects))}", silent=True)
    tid = store.create(project, prompt)
    await tg.send(f"📥 <b>#{tid}</b> kuyrukta · {esc(project)}", silent=True)
    worker.submit(tid)


async def plain(tg, worker, text):
    """A message that isn't a reply: a new task, unless the default project has tasks from the last day that
    could take it as a follow-up; then ask which. (Replies to a task's message go straight to it, never here.)"""
    project = next(iter(cfg.projects()))
    since = time.time() - 24 * 3600  # ponytail: fixed window; older tasks are still reachable by replying
    open_ = [t for t in store.recent(30) if t["project"] == project and t["state"] in store.FOLLOWUP
             and t["updated"] > since and t["id"] not in worker.jobs][:3]
    if not open_:
        return await new_task(tg, worker, project, text)
    buttons = [(f"↩️ #{t['id']} · {' '.join(t['prompt'].split())[:40]}", f"n:{t['id']}") for t in open_]
    mid = (await tg.send(f"❓ Yeni görev mi, devam mı?\n<i>{esc(clip(text, 500))}</i>\n\n"
                         f"<i>Görevin mesajını yanıtlarsan sormadan devam ederim.</i>",
                         buttons + [("🆕 Yeni görev", "n:new")], silent=True))["message_id"]
    routes[mid] = (project, text)


async def route(tg, worker, mid, pick):
    """The tap on plain()'s question."""
    job = routes.pop(mid, None)
    if not job:  # double tap
        return
    project, text = job
    if pick == "new":
        await tg.edit(mid, f"🆕 Yeni görev\n<i>{esc(clip(text, 500))}</i>")
        return await new_task(tg, worker, project, text)
    await tg.edit(mid, f"↩️ <b>#{pick}</b>'e devam\n<i>{esc(clip(text, 500))}</i>")
    await worker.followup(int(pick), text)


def use_project(name):
    projects = cfg.projects()
    if name not in projects:
        return f"Proje yok: {esc(name)}. Mevcut: {esc(', '.join(projects))}"
    cfg.save_projects({name: projects[name], **projects})  # first entry = default
    return f"⭐ Varsayılan proje: <b>{esc(name)}</b> · <code>{esc(projects[name]['path'])}</code>"


async def add_project(name, path):
    path = os.path.abspath(path.strip().strip("\"'"))
    if name in cfg.projects():
        return f"'{esc(name)}' zaten var. Başka ad seç ya da /use {esc(name)}"
    if not os.path.isdir(path):
        return f"Klasör yok: <code>{esc(path)}</code>"
    if policy.inside(path, cfg.ROOT) or policy.inside(cfg.ROOT, path):  # agent must never get a copy of Engent itself
        return "Bu klasör Engent'i içeriyor ya da onun içinde; proje olamaz."
    note = ""
    try:
        if await git(path, "rev-parse", "--git-dir", check=False) is None:
            if not os.path.exists(os.path.join(path, ".gitignore")):  # keep deps and secrets out of every worktree
                with open(os.path.join(path, ".gitignore"), "w", encoding="utf-8") as f:
                    f.write(GITIGNORE)
            await git(path, "init", "-b", "main")
            await git(path, "add", "-A")
            await git(path, *await ident(path), "commit", "--allow-empty", "-m", "engent: ilk commit")
            note = "\n🆕 git reposu oluşturuldu (.gitignore + ilk commit)."
        base = await git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    except Failure as e:
        return f"⚠️ Eklenemedi: {esc(clip(str(e), 500))}"
    if not base or base == "HEAD":
        return "⚠️ Repoda commit yok ya da HEAD bir dala bağlı değil; önce masada bir dal checkout et."
    cfg.save_projects({**cfg.projects(), name: {"path": path, "base": base, "engine": "claude"}})
    return (f"➕ <b>{esc(name)}</b> eklendi · dal <code>{esc(base)}</code>{note}\n"
            f"Varsayılan yapmak için: /use {esc(name)}\n"
            f"<i>Test/deploy komutları için projects.json'a \"test\" / \"deploy\" ekleyebilirsin.</i>")


def unlocked():
    """Password gate: after LOCK_AFTER without a message or tap, BOT_PASSWORD is asked before anything else.
    Last activity is data/seen's mtime, so a restart doesn't reopen the gate."""
    seen = cfg.DATA / "seen"
    try:
        ok = not cfg.BOT_PASSWORD or time.time() - seen.stat().st_mtime < cfg.LOCK_AFTER
    except OSError:
        ok = False
    if ok:
        seen.touch()
    return ok


MAX_FAILS = 5
UNLOCK = cfg.ROOT / "unlock.cmd"


def strike():
    """One more wrong password -> tries left. Kept in data/fails (only the right password resets it, a restart
    doesn't); the last one creates the lockout file here, synchronously, so parallel guesses can't slip past."""
    fails = cfg.DATA / "fails"
    try:
        n = int(fails.read_text()) + 1
    except (OSError, ValueError):
        n = 1
    fails.write_text(str(n))
    if n >= MAX_FAILS:
        (cfg.DATA / "lockout").touch()
        fails.unlink()
    return MAX_FAILS - n


async def forget(tg, msg):
    """Password attempts don't stay in the chat history (a bot can delete a private-chat message for 48 h)."""
    try:
        await tg.call("deleteMessage", chat_id=msg["chat"]["id"], message_id=msg["message_id"], force=True)
    except RuntimeError as e:
        log.warning("şifre mesajı silinemedi: %s", e)


async def handle(u, tg, worker):
    if cfg.locked_out():  # MAX_FAILS wrong passwords: nothing is read (tg sends nothing) until unlock.cmd
        return
    if "callback_query" in u:
        cq = u["callback_query"]
        if cq["from"]["id"] not in cfg.ALLOWED_USERS:
            return
        if not unlocked():  # before on_callback: the approval stays pending until the password comes
            return await tg.call("answerCallbackQuery", callback_query_id=cq["id"], show_alert=True,
                                 text="🔒 Kilitli: önce şifreyi mesaj olarak yaz, sonra butona tekrar bas.")
        data = cq.get("data", "")
        mid = cq.get("message", {}).get("message_id")
        # menus live in memory, so their buttons go stale on restart: point at the command that reopens them
        menu, again = {"f": (listings, "/ls"), "m": (model_menus, "/model"),
                       "e": (model_menus, "/model"), "x": (runs, "/run"),
                       "n": (routes, "Mesajını tekrar")}.get(data[:1], (None, ""))
        stale = (data.startswith("a:") and not tg.on_callback(data)) or (menu is not None and mid not in menu)
        await tg.call("answerCallbackQuery", callback_query_id=cq["id"],
                      text=("Bu istek artık geçerli değil." + (f" {again} yaz." if again else "")) if stale else "")
        if data.startswith("t:"):
            _, act, tid = data.split(":")
            await worker.action(act, int(tid))
        elif data.startswith("f:") and not stale:
            await tap_folder(tg, mid, data[2:])
        elif data[:2] in ("m:", "e:") and not stale:
            await tap_model(tg, mid, data)
        elif data.startswith("x:") and not stale:
            await run_command(tg, mid)
        elif data.startswith("n:") and not stale:
            await route(tg, worker, mid, data[2:])
        elif data.startswith("p:") and data[2:] in {k for k, _, _ in AUTONOMY}:
            cfg.save_autonomy(data[2:])
            await tg.edit(mid, *autonomy_menu())
        return

    msg = u.get("message") or {}
    if msg.get("from", {}).get("id") not in cfg.ALLOWED_USERS:
        if msg:
            log.warning("yetkisiz kullanıcı yok sayıldı: %s", msg.get("from"))
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    # No await from the lockout check down to strike(): a burst of guesses is counted before any of them is answered.
    if hmac.compare_digest(text.encode(), cfg.BOT_PASSWORD.encode()):  # also when open: never becomes a task
        (cfg.DATA / "fails").unlink(missing_ok=True)
        (cfg.DATA / "seen").touch()
        await forget(tg, msg)
        return await tg.send("🔓 Açık. Komutunu gönderebilirsin.", silent=True)
    if not unlocked():  # while locked every message is a password attempt
        left = strike()
        await forget(tg, msg)
        if left:
            return await tg.send(f"🔒 Kilitli ({cfg.LOCK_AFTER / 60:.0f} dk mesaj yoktu): şifreyi yaz. "
                                 f"Kalan hak: {left}", silent=True)
        log.warning("%d kez yanlış şifre: bot kapandı, açmak için %s çalıştırılmalı", MAX_FAILS, UNLOCK)
        return await tg.send(f"⛔ <b>{MAX_FAILS} kez yanlış şifre: bot kapandı.</b>\nBundan sonra hiçbir mesaja ve "
                             f"butona cevap vermeyecek, bildirim de göndermeyecek; onay bekleyen işler reddedilecek."
                             f"\n\nYeniden açmak için bilgisayarda şunu çalıştır:\n<code>{esc(UNLOCK)}</code>",
                             force=True)
    replied = msg.get("reply_to_message") or {}
    if replied:
        if tg.on_reply(replied["message_id"], text):
            return
        # the task's last message, or any other message naming exactly one task (📥 #12 kuyrukta, an old approval…)
        named = set(re.findall(r"#(\d+)", replied.get("text") or ""))
        task = store.by_msg(replied["message_id"]) or (store.get(int(named.pop())) if len(named) == 1 else None)
        if task and not text.startswith("/"):  # a command stays a command even as a reply
            return await worker.followup(task["id"], text)

    cmd, _, rest = text.partition(" ")
    cmd, rest = cmd.lower().split("@")[0], rest.strip()
    arg = int(rest) if rest.isdigit() else None
    if cmd in ("/start", "/help"):
        await tg.send(HELP, silent=True)
    elif cmd == "/status":
        await tg.send(status_text(worker.engines), silent=True)
    elif cmd == "/usage":
        await tg.send(await worker.usage(), silent=True)
    elif cmd == "/model":
        await model_menu(tg)
    elif cmd == "/izin":
        await tg.send(*autonomy_menu(), silent=True)
    elif cmd == "/compact" and arg:
        await worker.compact(arg)
    elif cmd == "/projects":
        await tg.send("\n".join(f"{'⭐' if i == 0 else '•'} <b>{esc(n)}</b> ({esc(p.get('engine', 'claude'))}) "
                                f"<code>{esc(p['path'])}</code>" for i, (n, p) in enumerate(cfg.projects().items()))
                      + "\n\n<i>/use ad → varsayılanı değiştir · /ls → klasörlerde gezip seç</i>", silent=True)
    elif cmd == "/use" and rest:
        await tg.send(use_project(rest), silent=True)
    elif cmd == "/add" and rest:
        name, _, path = rest.partition(" ")
        await tg.send(await add_project(name, resolve(path) if path.strip() else nav["cwd"]), silent=True)
    elif cmd in ("/ls", "/dir"):
        await show(tg, resolve(rest or "."))
    elif cmd == "/cd":
        await show(tg, resolve(rest))
    elif cmd == "/run" and rest:
        await confirm_run(tg, rest)
    elif cmd == "/log" and arg:
        await tg.send(f"<pre>{esc(clip(log_tail(arg), 3500)) or '(boş)'}</pre>", silent=True)
    elif cmd == "/stop" and arg:
        await worker.stop(arg)
    elif cmd == "/stop":
        running = [(f"⏹ #{t} · {' '.join(store.get(t)['prompt'].split())[:40]}", f"t:s:{t}")
                   for t in sorted(worker.jobs)]
        await tg.send("Hangisi durdurulsun?" if running else "Şu an çalışan ajan yok.", running, silent=True)
    elif cmd == "/new" and " " in rest:
        project, _, prompt = rest.partition(" ")
        await new_task(tg, worker, project, prompt.strip())
    elif cmd.startswith("/"):
        await tg.send(HELP, silent=True)
    else:
        await plain(tg, worker, text)


async def main():
    tg = Telegram(cfg.BOT_TOKEN, cfg.ALLOWED_USERS)
    worker = Worker(tg)
    missing = [x for x in ("git",) if not shutil.which(x)]
    try:  # own try: the "/" menu must refresh even when the chat isn't reachable yet
        await tg.call("setMyCommands", commands=MENU)
    except RuntimeError as e:
        log.warning("komut menüsü ayarlanamadı: %s", e)
    try:
        await tg.send("🟢 <b>Engent çevrimiçi</b>" + (f"\n⚠️ PATH'te yok: {', '.join(missing)}" if missing else ""),
                      silent=True)
        await worker.recover()
    except RuntimeError as e:  # e.g. "chat not found" until the user has sent /start once; keep polling anyway
        log.warning("bildirim gönderilemedi (bota /start yazıldı mı?): %s", e)
    inflight = set()

    async def guarded(u):
        try:
            await handle(u, tg, worker)
        except Exception:
            log.exception("update işlenemedi: %s", u.get("update_id"))

    async for u in tg.updates():  # each update in its own task: an approval tap is never stuck behind a merge
        job = asyncio.create_task(guarded(u))
        inflight.add(job)
        job.add_done_callback(inflight.discard)


def run():
    lock = open(cfg.DATA / "engent.lock", "w")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.exit("Engent zaten çalışıyor.")
    handlers = [logging.handlers.RotatingFileHandler(cfg.DATA / "engent.log", maxBytes=5_000_000, backupCount=3,
                                                     encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain the bot token in the URL
    if not cfg.BOT_TOKEN or not cfg.ALLOWED_USERS:
        sys.exit(".env: TELEGRAM_BOT_TOKEN ve TELEGRAM_ALLOWED_USERS gerekli.")
    while True:  # self-supervision; Task Scheduler restarts the process if it ever dies anyway
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            return
        except Exception:
            log.exception("ana döngü çöktü; 10 sn sonra yeniden başlıyor")
            time.sleep(10)


if __name__ == "__main__":
    run()
