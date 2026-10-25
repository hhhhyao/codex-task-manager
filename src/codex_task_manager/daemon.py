import asyncio
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal

from .common import private_directory, socket_path
from .manager import Manager
from .storage import Store
from .transport import send


MAX_REQUEST = 262144


async def run_check(spec, on_start):
    process = None
    async def bounded(stream):
        chunks, size = [], 0
        while chunk := await stream.read(8192):
            size += len(chunk)
            if size > 65536:
                raise ValueError("check output exceeds 64 KiB; publish brief events and reference source files")
            chunks.append(chunk)
        return b"".join(chunks)
    try:
        process = await asyncio.create_subprocess_exec(*spec["check"], cwd=spec["cwd"],
                   stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                   stderr=asyncio.subprocess.PIPE, start_new_session=True,
                   env={**os.environ, "CODEX_TASK_MANAGER_TASK_ID": spec["id"]})
        on_start(process.pid)
        code, out, err = await asyncio.wait_for(asyncio.gather(process.wait(), bounded(process.stdout), bounded(process.stderr)), spec["check_timeout"])
        if code == 1 and not out.strip():
            return {"events": [], "status": "pending"}, None
        if code:
            return None, f"check exit {code}: " + err.decode(errors="replace")[:500]
        return json.loads(out), None
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError, asyncio.TimeoutError) as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


class Daemon:
    def __init__(self, state_dir, max_checks=4, max_sends=4, retention=300):
        self.state_dir, self.retention = state_dir, retention
        self.max_checks, self.max_sends = max_checks, max_sends
        self.store = Store(state_dir)
        self.manager = Manager(self.store)
        self.checks, self.sends = {}, {}
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.path = socket_path(state_dir)
        self.logger = logging.getLogger("codex-task-manager")

    async def handle(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), 10)
            if len(raw) > MAX_REQUEST:
                raise ValueError("request too large")
            request = json.loads(raw)
            result = await self.dispatch(request)
            if request.get("op") not in {"ping", "show", "list"}:
                self.wake.set()
            response = {"ok": True, "result": result}
        except (ValueError, TypeError, KeyError, OSError, asyncio.TimeoutError) as exc:
            response = {"ok": False, "error": str(exc)[:1500]}
        try:
            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def dispatch(self, request):
        op = request["op"]
        task_id = request.get("id")
        if op == "ping":
            active = self.manager.list()
            return {"pid": os.getpid(), "state_dir": str(self.state_dir), "active_tasks": len(active),
                    "checks_running": len(self.checks), "sends_running": len(self.sends)}
        if op == "submit":
            return self.manager.submit(request["spec"])
        if op == "list":
            return self.manager.list(request.get("owner"), request.get("include_finished", False))
        if op == "show":
            return self.manager.show(task_id, request.get("buffer", False))
        if op == "emit":
            return self.manager.emit(task_id, request["events"])
        if op == "flush":
            return self.manager.flush(task_id)
        if op == "review":
            return self.manager.configure_review(task_id, request["interval_seconds"])
        if op == "resume":
            return self.manager.resume(task_id)
        if op == "resolve":
            return self.manager.resolve(task_id, request["outcome"])
        if op == "cancel":
            result = self.manager.cancel(task_id)
            pending = self.checks.get(task_id)
            if pending:
                pending.cancel()
            # A send already handed to HTTP cannot be recalled; its result is ignored.
            return result
        if op == "stop":
            self.stop.set()
            return {"stopping": True}
        raise ValueError("unknown operation")

    async def check(self, spec):
        task_id = spec["id"]
        try:
            result, error = await run_check(spec, lambda pid: self.manager.check_started(task_id, pid))
            self.manager.check_result(task_id, result, error)
        finally:
            self.checks.pop(task_id, None)
            self.wake.set()

    async def deliver(self, spec, batch):
        try:
            status, error = await asyncio.to_thread(send, spec, batch)
            self.manager.send_result(spec["id"], batch["id"], status, error)
        finally:
            self.sends.pop(spec["id"], None)
            self.wake.set()

    async def loop(self):
        while not self.stop.is_set():
            self.wake.clear()
            self.manager.tick()
            now = self.manager.clock()
            records = sorted(self.store.all(), key=lambda item: item[1]["next_check_at"] if item[1]["next_check_at"] is not None else float("inf"))
            for spec, state in records:
                task_id = spec["id"]
                if state["terminal"]:
                    continue
                if ("check" in spec and not state["paused"] and not state["source_done"] and
                        state["next_check_at"] is not None and state["next_check_at"] <= now and
                        task_id not in self.checks and len(self.checks) < self.max_checks):
                    self.manager.begin_check(task_id)
                    self.checks[task_id] = asyncio.create_task(self.check(spec))
                if not state["outbox"]:
                    self.manager.freeze(task_id)
                _, current = self.store.get(task_id)
                batch = current["outbox"]
                if (batch and task_id not in self.sends and len(self.sends) < self.max_sends and
                        batch["status"] in {"ready", "rejected"} and batch["next_attempt_at"] <= now and
                        batch["attempts"] < spec["max_attempts"]):
                    frozen = self.manager.begin_send(task_id)
                    self.sends[task_id] = asyncio.create_task(self.deliver(spec, frozen))
            self.manager.cleanup(self.retention)
            deadlines = [now + 30]
            for spec, state in self.store.all():
                if state["terminal"]:
                    deadlines.append(state["terminal_at"] + self.retention)
                    continue
                if not state["source_done"] and not state["paused"] and state["next_check_at"] is not None and len(self.checks) < self.max_checks:
                    deadlines.append(state["next_check_at"])
                if not state["outbox"] and state["next_flush_at"] is not None:
                    deadlines.append(state["next_flush_at"])
                batch = state["outbox"]
                if batch and batch["status"] in {"ready", "rejected"} and batch["attempts"] < spec["max_attempts"] and len(self.sends) < self.max_sends:
                    deadlines.append(batch["next_attempt_at"])
                if state["next_review_at"] is not None and not state["source_done"]:
                    # An already-due review awaiting a current check is woken by its result.
                    if state["next_review_at"] > now:
                        deadlines.append(state["next_review_at"])
            try:
                await asyncio.wait_for(self.wake.wait(), max(0.01, min(deadlines) - self.manager.clock()))
            except asyncio.TimeoutError:
                pass

    async def run(self):
        for directory in (self.path.parent.parent, self.path.parent):
            private_directory(directory)
        lock = (self.state_dir / "daemon.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            self.store.close()
            raise ValueError("a daemon already owns this state directory")
        self.path.unlink(missing_ok=True)
        self.manager.recover()
        server = await asyncio.start_unix_server(self.handle, path=self.path, limit=MAX_REQUEST)
        self.path.chmod(0o600)
        def stopping():
            self.stop.set()
            self.wake.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stopping)
        self.logger.info("daemon started pid=%s", os.getpid())
        try:
            async with server:
                await self.loop()
        finally:
            server.close()
            await server.wait_closed()
            checks = list(self.checks.items())
            for _, task in checks:
                task.cancel()
            await asyncio.gather(*(task for _, task in checks), return_exceptions=True)
            # Normal shutdown is known to kill the checks, so they can run again.
            for task_id, _ in checks:
                with self.store.edit(task_id) as (_, state):
                    state.update(checking=False, check_pid=None)
                    if not state["terminal"] and not state["source_done"]:
                        state["next_check_at"] = self.manager.clock()
            # Give bounded HTTP attempts time to finish and persist their outcome.
            await asyncio.gather(*list(self.sends.values()), return_exceptions=True)
            self.path.unlink(missing_ok=True)
            try:
                self.path.parent.rmdir()
            except OSError:
                pass
            self.store.close()
            lock.close()
            self.logger.info("daemon stopped")


def run_daemon(state_dir, max_checks=4, max_sends=4, retention=300):
    private_directory(state_dir)
    handler = RotatingFileHandler(state_dir / "daemon.log", maxBytes=131072, backupCount=1)
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(Daemon(state_dir, max_checks, max_sends, retention).run())
