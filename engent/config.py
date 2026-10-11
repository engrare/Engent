"""Settings. Secrets are read from .env into ENV but never exported to os.environ,
so agent/test processes only receive the keys handed to them explicitly."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
(DATA / "logs").mkdir(parents=True, exist_ok=True)


def _read_env(path):
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, val = line.partition("=")
            if sep and not key.strip().startswith("#"):
                env[key.strip()] = val.strip().strip("\"'")
    return env


ENV = _read_env(ROOT / ".env")


def _get(key, default=""):
    return ENV.get(key) or os.environ.get(key) or default


def _csv(key, default=""):
    return [x.strip() for x in _get(key, default).split(",") if x.strip()]


BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = [int(x) for x in _csv("TELEGRAM_ALLOWED_USERS")]
BOT_PASSWORD = _get("BOT_PASSWORD")  # empty = no lock
LOCK_AFTER = 60 * float(_get("LOCK_AFTER_MIN", "60"))
MODEL = _get("ENGENT_MODEL") or None  # None = Claude Code's default model
MAX_PARALLEL = int(_get("MAX_PARALLEL", "2"))
MAX_TURNS = int(_get("MAX_TURNS", "200"))
MAX_BUDGET_USD = float(_get("MAX_BUDGET_USD")) if _get("MAX_BUDGET_USD") else None
MAX_FIX_ATTEMPTS = int(_get("MAX_FIX_ATTEMPTS", "3"))
TASK_TIMEOUT = 60 * float(_get("TASK_TIMEOUT_MIN", "180"))
APPROVAL_TIMEOUT = 60 * float(_get("APPROVAL_TIMEOUT_MIN", "240"))
CMD_TIMEOUT = 60 * float(_get("CMD_TIMEOUT_MIN", "20"))
WORKTREES = Path(_get("WORKTREE_DIR", str(Path.home() / "engent-worktrees")))
# Only these .env keys reach the agent process (its own auth). Telegram/deploy tokens never do.
AGENT_ENV = {k: ENV[k] for k in _csv("AGENT_ENV_KEYS", "ANTHROPIC_API_KEY,CLAUDE_CODE_OAUTH_TOKEN") if ENV.get(k)}


def projects():
    """Re-read on every call so projects.json edits apply without a restart. First entry = default."""
    data = json.loads((ROOT / "projects.json").read_text(encoding="utf-8"))
    for p in data.values():
        p.setdefault("base", "main")
    return data


def model_choice():
    """Model/effort picked with /model on the phone, else .env. Read at every agent start: no restart needed."""
    try:
        return json.loads((DATA / "model.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"model": MODEL}


def save_model(choice):
    (DATA / "model.json").write_text(json.dumps(choice, ensure_ascii=False), encoding="utf-8")


def locked_out():
    """Wrong password MAX_FAILS times: the bot reads and sends nothing until unlock.cmd (on the PC) deletes this file."""
    return (DATA / "lockout").exists()


def autonomy():
    """/izin level: ask | edits | auto (anything else acts as ask). Read at every tool call: reaches running agents."""
    try:
        return (DATA / "autonomy.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "ask"


def save_autonomy(level):
    (DATA / "autonomy.txt").write_text(level, encoding="utf-8")


def save_projects(data):
    (ROOT / "projects.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
