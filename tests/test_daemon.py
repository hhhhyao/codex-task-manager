import concurrent.futures
import datetime
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_task_manager.client import request
from codex_task_manager.common import socket_path, stamp
from codex_task_manager.transport import classify
from codex_task_manager.daemon import run_check
from test_manager import OWNER


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.messages.append(value)
        if self.server.delay:
            time.sleep(self.server.delay)
        self.send_response(self.server.code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps({"ok": True, "hook_delivery": {"status": "accepted"}}).encode())
        except BrokenPipeError:
            pass


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.messages, self.server.code, self.server.delay = [], 200, 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                    "CODEX_TASK_MANAGER_REPOWIRE_URL": f"http://127.0.0.1:{self.server.server_port}",
                    "REPOWIRE_DAEMON__AUTH_TOKEN": "fake-test-token"}
        self.process = None
        self.start()
        self.addCleanup(self.stop)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "codex_task_manager", "--state-dir", str(self.state), *args],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def wait_for(self, predicate, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            time.sleep(0.025)
        self.fail("condition did not become true within test deadline")

    def start(self):
        self.process = subprocess.Popen([sys.executable, "-m", "codex_task_manager", "--state-dir", str(self.state), "daemon", "run"],
                          env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        def ready():
            if self.process.poll() is not None:
                self.fail(self.process.communicate()[1])
            try:
                return request(self.state, "ping")
            except ValueError:
                return None
        self.wait_for(ready)

    def stop(self):
        if self.process and self.process.poll() is None:
            request(self.state, "stop")
            _, error = self.process.communicate(timeout=20)
            self.assertEqual(self.process.returncode, 0, error)
        elif self.process:
            self.process.communicate()

    def submit(self, **kwargs):
        return request(self.state, "submit", spec={"name": "fake-work", "owner": OWNER,
                       "cwd": str(self.root), "mode": "repeat", "window_seconds": 0.15, **kwargs})["id"]

    def test_multiple_agents_submit_and_callbacks_batch_into_direct_readable_message(self):
        def submit(index):
            return self.submit(owner={**OWNER, "thread_id": str(uuid.uuid4()), "label": f"agent-{index}"})
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            tasks = list(pool.map(submit, range(4)))
        self.assertEqual(len(request(self.state, "list")), 4)
        for index, task in enumerate(tasks):
            for name in ("A", "B"):
                request(self.state, "emit", id=task, events=[{"id": name, "text": f"Result {name} completed for task {index}"}])
        self.wait_for(lambda: len(self.server.messages) == 4)
        for body in self.server.messages:
            self.assertIn("Result A completed", body["text"])
            self.assertIn("Result B completed", body["text"])
            self.assertIn("[sent_at:", body["text"])
            self.assertNotIn("collect", body["text"])
        self.assertTrue(all(request(self.state, "show", id=task)["buffered"] == 0 for task in tasks))

    def test_real_pending_script_gets_multiple_reviews_then_finishes_normally(self):
        script = self.root / "pending.py"
        script.write_text("import json;from pathlib import Path\n"
                          "value={'events':[{'id':'ready','text':'Real result completed','immediate':True}], 'done':True, 'status':'completed'} if Path('ready').exists() else {'events':[], 'status':'still waiting'}\n"
                          "print(json.dumps(value))")
        task = self.submit(mode="once", check=[sys.executable, str(script)], interval_seconds=0.05,
                           review_interval_seconds=0.2)
        self.wait_for(lambda: len(self.server.messages) >= 2)
        self.assertEqual(request(self.state, "show", id=task)["review_count"], 2)
        self.assertFalse(request(self.state, "show", id=task)["source_done"])
        (self.root / "ready").touch()
        self.wait_for(lambda: request(self.state, "show", id=task)["status"] == "finished")
        self.assertEqual(len(self.server.messages), 3)
        self.assertIn("Waiting review 1", self.server.messages[0]["text"])
        self.assertIn("Waiting review 2", self.server.messages[1]["text"])
        self.assertIn("Real result completed", self.server.messages[2]["text"])
        time.sleep(0.3)
        self.assertEqual(len(self.server.messages), 3)

    def test_restart_preserves_unflushed_buffer_without_history(self):
        task = self.submit(window_seconds=30)
        request(self.state, "emit", id=task, events=[{"id": "A", "text": "A completed"}])
        self.stop()
        self.start()
        self.assertEqual(request(self.state, "show", id=task)["buffered"], 1)
        request(self.state, "flush", id=task)
        self.wait_for(lambda: request(self.state, "show", id=task)["last_sent_at"])
        self.assertEqual(len(self.server.messages), 1)
        self.assertIsNone(request(self.state, "show", id=task)["delivery"])

    def test_unknown_send_outcome_is_not_automatically_retried(self):
        self.server.code = 503
        task = self.submit(retry_seconds=0.05)
        request(self.state, "emit", id=task, events=[{"id": "A", "text": "A completed"}])
        self.wait_for(lambda: request(self.state, "show", id=task)["status"] == "delivery_ambiguous")
        self.server.code = 200
        time.sleep(0.3)
        self.assertEqual(len(self.server.messages), 1)
        request(self.state, "resolve", id=task, outcome="not-delivered")
        self.wait_for(lambda: request(self.state, "show", id=task)["last_sent_at"])
        self.assertEqual(len(self.server.messages), 2)

    def test_rejected_queue_does_not_block_healthy_queue_and_retries_are_bounded(self):
        self.server.code = 400
        bad = self.submit(retry_seconds=0.05, max_attempts=2)
        request(self.state, "emit", id=bad, events=[{"id": "bad", "text": "Diagnose failure"}])
        self.wait_for(lambda: len(self.server.messages) == 2)
        time.sleep(0.2)
        self.assertEqual(len(self.server.messages), 2)
        self.server.code = 200
        good = self.submit()
        request(self.state, "emit", id=good, events=[{"id": "good", "text": "Healthy queue result"}])
        self.wait_for(lambda: request(self.state, "show", id=good)["last_sent_at"])
        self.assertEqual(request(self.state, "show", id=bad)["status"], "delivery_rejected")

    def test_cancel_running_observer_kills_check_group_but_not_external_workload(self):
        marker = self.root / "should-not-exist"
        task = self.submit(check=[sys.executable, "-c", f"import time;from pathlib import Path;time.sleep(1);Path({str(marker)!r}).touch()"], check_timeout=3)
        self.wait_for(lambda: request(self.state, "show", id=task)["check_pid"])
        request(self.state, "cancel", id=task)
        time.sleep(1.2)
        self.assertFalse(marker.exists())
        self.assertEqual(self.server.messages, [])

    def test_cli_submission_listing_and_owner_filter(self):
        path = self.root / "task.json"
        path.write_text(json.dumps({"name": "CLI task", "description": "Observe fake evaluation results", "owner": OWNER, "mode": "repeat"}))
        result = self.cli("submit", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        task = json.loads(result.stdout)["id"]
        listing = self.cli("list", "--owner", OWNER["thread_id"], "--json")
        self.assertEqual([r["id"] for r in json.loads(listing.stdout)], [task])
        self.assertIn("CLI task", self.cli("list").stdout)
        self.assertIn("Observe fake evaluation results", self.cli("list").stdout)
        described = self.cli("describe", task, "Compare background run outcomes")
        self.assertEqual(described.returncode, 0, described.stderr)
        self.assertEqual(json.loads(described.stdout)["description"], "Compare background run outcomes")
        self.stop()
        self.start()
        self.assertEqual(json.loads(self.cli("show", task).stdout)["description"], "Compare background run outcomes")
        self.assertEqual(self.cli("cancel", task).returncode, 0)


class ReceiptTests(unittest.TestCase):
    def test_nested_failure_wins_over_legacy_delivered_flag(self):
        self.assertEqual(classify({"delivered": True, "hook_delivery": {"status": "failed"}}), "rejected")
        self.assertEqual(classify({"ok": True}), "ambiguous")
        self.assertEqual(classify({"queued": True}), "queued")
        self.assertEqual(classify({"hook_delivery": {"status": []}}), "ambiguous")


class CheckProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_stdout_is_read_completely(self):
        with tempfile.TemporaryDirectory() as directory:
            code = "import sys,time;sys.stdout.write('{\"events\":');sys.stdout.flush();time.sleep(.05);print('[],\"status\":\"running\"}')"
            result, error = await run_check({"check": [sys.executable, "-c", code], "cwd": directory,
                                            "check_timeout": 2, "id": "test"}, lambda pid: None)
            self.assertIsNone(error)
            self.assertEqual(result["status"], "running")

    async def test_oversized_check_output_is_bounded_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            result, error = await run_check({"check": [sys.executable, "-c", "print('x'*100000)"], "cwd": directory,
                                            "check_timeout": 2, "id": "test"}, lambda pid: None)
            self.assertIsNone(result)
            self.assertIn("64 KiB", error)

    async def test_timeout_kills_descendants(self):
        import asyncio
        with tempfile.TemporaryDirectory() as directory:
            child = "from pathlib import Path;import time;time.sleep(.4);Path('orphan').touch()"
            code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(3)"
            result, error = await run_check({"check": [sys.executable, "-c", code], "cwd": directory,
                                            "check_timeout": .1, "id": "test"}, lambda pid: None)
            self.assertIsNone(result)
            self.assertIn("Timeout", error)
            await asyncio.sleep(.5)
            self.assertFalse((Path(directory) / "orphan").exists())


if __name__ == "__main__":
    unittest.main()
