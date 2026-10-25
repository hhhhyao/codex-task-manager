"""Only current task state; successful message bodies are deleted, not archived."""

from contextlib import contextmanager
import json
import sqlite3

from .common import private_directory


class Store:
    def __init__(self, directory):
        private_directory(directory)
        self.path = directory / "tasks.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=5)
        self.path.chmod(0o600)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, spec TEXT NOT NULL, state TEXT NOT NULL)")
        self.db.commit()

    def close(self):
        self.db.close()

    def insert(self, spec, state):
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?, ?, ?)",
                            (spec["id"], json.dumps(spec), json.dumps(state)))

    def get(self, task_id):
        row = self.db.execute("SELECT spec, state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("unknown task ID")
        return json.loads(row[0]), json.loads(row[1])

    def all(self):
        return [(json.loads(a), json.loads(b)) for a, b in self.db.execute("SELECT spec,state FROM tasks")]

    @contextmanager
    def edit(self, task_id):
        # All accesses run on the daemon's event-loop thread, never across awaits.
        spec, state = self.get(task_id)
        original_spec = json.dumps(spec)
        original = json.dumps(state)
        yield spec, state
        updated = json.dumps(state)
        updated_spec = json.dumps(spec)
        if updated != original or updated_spec != original_spec:
            with self.db:
                self.db.execute("UPDATE tasks SET spec=?,state=? WHERE id=?", (updated_spec, updated, task_id))

    def delete(self, task_id):
        with self.db:
            self.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
