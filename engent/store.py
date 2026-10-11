"""Task state in SQLite (stdlib). It outlives the process, which is what makes resume-after-crash possible."""
import sqlite3
import time

from .config import DATA

ACTIVE = ("queued", "running", "waiting", "testing", "fixing", "deploying")
RESUMABLE = ("failed", "cancelled", "interrupted")
FOLLOWUP = RESUMABLE + ("review", "done")

db = sqlite3.connect(DATA / "engent.db", isolation_level=None, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("""CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, project TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL,
    branch TEXT, worktree TEXT, session_id TEXT, url TEXT, note TEXT, msg_id INTEGER,
    created REAL, updated REAL)""")
for col in ("cost REAL", "ctx INTEGER", "ctx_max INTEGER"):  # usage columns, added to existing DBs in place
    try:
        db.execute(f"ALTER TABLE tasks ADD COLUMN {col}")
    except sqlite3.OperationalError:  # already there
        pass


def create(project, prompt):
    now = time.time()
    return db.execute(
        "INSERT INTO tasks (project, prompt, state, created, updated) VALUES (?, ?, 'queued', ?, ?)",
        (project, prompt, now, now),
    ).lastrowid


def get(tid):
    return db.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()


def update(tid, **fields):
    fields["updated"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)  # keys come from code, never from user input
    db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), tid))


def in_states(states):
    marks = ",".join("?" * len(states))
    return db.execute(f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY id", states).fetchall()


def recent(limit=12):
    return db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def by_msg(msg_id):
    return db.execute("SELECT * FROM tasks WHERE msg_id = ?", (msg_id,)).fetchone()
