import fcntl
from contextlib import closing
import http.server
import json
import os
from pathlib import Path
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unicodedata
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_task_manager import dashboard
from codex_task_manager.tui import Selection, status_role, terminal_palette, wrapped_detail


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.reads.append(self.path)
        self.send_response(self.server.health_code if self.path == "/health" else 200)
        self.end_headers()
        data = {"status": "ok", "peers": 1, "schema_version": 14, "auth_token": "never-show-secret"} if self.path == "/health" else {
            "peers": [{"peer_id": "peer-1", "display_name": "Fake agent", "backend": "codex", "status": "online",
                       "turn_state": "working", "source": "codex-app-server", "inbound_status": "online",
                       "metadata": {"auth_token": "never-show-secret"}, "certificate": "never-show-secret"},
                      {"peer_id": "peer-2", "display_name": "Offline agent", "status": "offline"}]}
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self):
        self.server.writes.append(self.path)
        self.send_response(405)
        self.end_headers()


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.reads, self.server.writes, self.server.health_code = [], [], 200
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def rpc(self, directory, op, timeout=10):
        self.assertIn(op, {"ping", "list"})
        if op == "ping":
            return {"pid": 123, "active_tasks": 1, "checks_running": 0, "sends_running": 0}
        return [{"id": "task-1", "name": "长时间工作", "description": "比较后台运行的评估结果", "owner": {"peer_id": "peer-1"}, "mode": "once",
                 "status": "waiting", "buffered": 2, "next_review_at": "2026-10-25T17:00:00Z",
                 "next_action": "Compare results", "unrecognized_secret": "never-show-secret"}]

    def snapshot(self):
        with patch.object(dashboard, "repowire_connection", return_value=(self.base, "test-token")), \
             patch.object(dashboard, "request", side_effect=self.rpc), \
             patch.object(dashboard, "processes", return_value={"repowire": [{"pid": 200}], "bridge": [{"pid": 201}], "app_server": []}):
            return dashboard.collect_snapshot(self.root)

    def test_snapshot_uses_only_reads_and_whitelisted_fields(self):
        value = self.snapshot()
        self.assertEqual(value["ctm"]["data"]["pid"], 123)
        self.assertEqual(value["tasks"]["data"][0]["next_action"], "Compare results")
        self.assertEqual(value["tasks"]["data"][0]["description"], "比较后台运行的评估结果")
        self.assertNotIn("never-show-secret", json.dumps(value))
        self.assertEqual(set(self.server.reads), {"/health", "/peers?include_hidden=true"})
        self.assertEqual(self.server.writes, [])
        self.assertEqual(value["services"][2]["status"], "running")
        self.assertEqual(value["services"][3]["status"], "not observed")

    def test_health_failure_does_not_hide_ctm_or_peer_list(self):
        self.server.health_code = 401
        value = self.snapshot()
        self.assertEqual(value["repowire"]["error"], "HTTP 401")
        self.assertEqual(len(value["tasks"]["data"]), 1)
        self.assertEqual(len(value["peers"]["data"]), 2)
        self.assertEqual(value["services"][1]["status"], "unavailable")
        self.assertEqual(value["services"][1]["pids"], [200])

    def test_missing_ctm_is_not_reported_as_an_empty_healthy_list(self):
        with patch.object(dashboard, "repowire_connection", return_value=(self.base, "test-token")), \
             patch.object(dashboard, "request", side_effect=ValueError("connection failed")), \
             patch.object(dashboard, "processes", side_effect=OSError("ps failed")):
            value = dashboard.collect_snapshot(self.root)
        self.assertIsNone(value["tasks"]["data"])
        self.assertIn("Unavailable", dashboard.text_snapshot(value))
        self.assertEqual(value["services"][2]["status"], "unknown")
        self.assertEqual(len(value["peers"]["data"]), 2)

    def test_config_failure_is_redacted_and_does_not_block_ctm(self):
        with patch.object(dashboard, "repowire_connection", side_effect=ValueError("secret=never-show-secret")), \
             patch.object(dashboard, "request", side_effect=self.rpc), \
             patch.object(dashboard, "processes", return_value={}):
            value = dashboard.collect_snapshot(self.root)
        self.assertEqual(len(value["tasks"]["data"]), 1)
        self.assertNotIn("never-show-secret", json.dumps(value))

    def test_search_offline_toggle_and_stable_selection(self):
        value = self.snapshot()
        self.assertEqual(len(dashboard.rows_for(value, "Agents")), 1)
        self.assertEqual(len(dashboard.rows_for(value, "Agents", show_all=True)), 2)
        self.assertEqual(len(dashboard.rows_for(value, "Tasks", "compare RESULTS")), 1)
        selected = Selection(1, "b")
        selected.sync([{"id": "b"}, {"id": "a"}])
        self.assertEqual(selected.index, 0)
        selected.sync([{"id": "a"}])
        self.assertEqual(selected.key, "a")

    def test_terminal_widths_and_control_characters(self):
        value = self.snapshot()
        self.assertNotIn("\x1b", dashboard.clip("name\x1b[2J", 30))
        for width in (55, 80, 120):
            for view in dashboard.VIEWS:
                lines = dashboard.table_lines(value, view, dashboard.rows_for(value, view), width)
                for line in lines:
                    size = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in line)
                    self.assertLessEqual(size, width)
        self.assertIn("17:00:00", dashboard.table_lines(value, "Tasks", value["tasks"]["data"], 120)[1])
        self.assertIn("比较后台运行的评估结果", dashboard.table_lines(value, "Tasks", value["tasks"]["data"], 120)[1])
        detail = wrapped_detail({"next_action": "验证中文排版" * 20}, 30)
        self.assertGreater(len(detail), 4)

    def test_session_names_match_exact_threads_without_exposing_metadata_or_prompts(self):
        with closing(sqlite3.connect(self.root/"state_10.sqlite")) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT)")
            conn.executemany("INSERT INTO threads VALUES (?, ?, ?)", [
                ("thread-1", "安装工具", "never-show-private-prompt"),
                ("thread-2", None, "never-show-private-prompt")])
        with closing(sqlite3.connect(self.root/"state_5.sqlite")) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT, name TEXT)")
            conn.execute("INSERT INTO threads VALUES ('thread-1', 'stale-name')")
        raw = {"peers": [
            {"peer_id": "a", "display_name": "code-3", "backend": "codex", "metadata": {
                "runtime_session_id": "thread-1", "agent_nickname": "Curie", "auth_token": "never-show-secret"}},
            {"peer_id": "b", "display_name": "code-4", "backend": "codex", "metadata": {"hook_session_id": "thread-2"}},
            {"peer_id": "remote", "display_name": "remote", "backend": "codex", "metadata": {"runtime_session_id": "remote-thread"}}]}
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}):
            peers = dashboard.project_peers(raw)
            self.assertEqual(peers[0]["session_name"], "安装工具")
            self.assertEqual(peers[0]["agent_nickname"], "Curie")
            self.assertNotIn("session_name", peers[1])
            self.assertNotIn("session_name", peers[2])
            self.assertNotIn("never-show", json.dumps(peers))
            with closing(sqlite3.connect(self.root/"state_10.sqlite")) as conn, conn:
                conn.execute("UPDATE threads SET name='重命名会话' WHERE id='thread-1'")
            updated = dashboard.project_peers(raw)
        self.assertEqual(updated[0]["session_name"], "重命名会话")
        self.assertEqual(dashboard.row_id(updated[0]), dashboard.row_id(peers[0]))
        snapshot = {"peers": {"data": peers}}
        self.assertEqual(len(dashboard.rows_for(snapshot, "Agents", "安装工具")), 1)
        wide = "\n".join(dashboard.table_lines(snapshot, "Agents", peers, 120))
        self.assertIn("安装工具", wide)
        self.assertIn("code-3", wide)
        narrow = "\n".join(dashboard.table_lines(snapshot, "Agents", peers, 70))
        self.assertIn("安装工具", narrow)
        self.assertIn("code-4", narrow)

    def test_optional_session_database_missing_or_incompatible_keeps_peers_available(self):
        missing = self.root/"missing"
        raw = {"peers": [{"peer_id": "a", "display_name": "Fallback", "backend": "codex",
                           "metadata": {"runtime_session_id": "thread-1"}}]}
        with patch.dict(os.environ, {"CODEX_HOME": str(missing)}):
            self.assertEqual(dashboard.project_peers(raw)[0]["display_name"], "Fallback")
            self.assertFalse(missing.exists())
        with closing(sqlite3.connect(self.root/"state_5.sqlite")) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT, title TEXT)")
            conn.execute("INSERT INTO threads VALUES ('thread-1', 'never-show-private-prompt')")
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}):
            self.assertNotIn("session_name", dashboard.project_peers(raw)[0])
            (self.root/"state_6.sqlite").write_bytes(b"corrupt")
            self.assertEqual(dashboard.project_peers(raw)[0]["display_name"], "Fallback")

    def test_non_tty_cli_snapshot_and_json_do_not_start_services(self):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]/"src"),
               "CODEX_TASK_MANAGER_REPOWIRE_URL": self.base, "REPOWIRE_DAEMON__AUTH_TOKEN": "test-token"}
        command = [sys.executable, "-m", "codex_task_manager", "--state-dir", str(self.root), "ui"]
        text = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertLess(text.stdout.index("Tasks"), text.stdout.index("Agents"))
        self.assertLess(text.stdout.index("Agents"), text.stdout.index("Services"))
        result = subprocess.run(command+["--json"], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["peers"]["data"][0]["peer_id"], "peer-1")
        self.assertFalse((self.root/"tasks.sqlite3").exists())
        self.assertEqual(self.server.writes, [])

    def test_actual_interactive_terminal_navigation_resize_and_exit(self):
        try:
            import curses
        except ImportError:
            self.skipTest("curses unavailable")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 120, 0, 0))
        env = {**os.environ, "TERM": "xterm-256color", "NO_COLOR": "", "PYTHONPATH": str(Path(__file__).resolve().parents[1]/"src"),
               "CODEX_TASK_MANAGER_REPOWIRE_URL": self.base, "REPOWIRE_DAEMON__AUTH_TOKEN": "test-token"}
        child = subprocess.Popen([sys.executable, "-m", "codex_task_manager", "--state-dir", str(self.root), "ui", "--refresh", "1"],
                                 env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        output = bytearray()
        def drain(seconds):
            until = time.monotonic()+seconds
            while time.monotonic()<until:
                if select.select([master], [], [], .05)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
        try:
            drain(.7)
            os.write(master, b"\x1bOC")  # Right: Tasks -> Agents (xterm application cursor mode).
            drain(.2)
            self.assertNotIn(b"Offline agent", output)
            os.write(master, b"a")
            drain(.2)
            self.assertIn(b"Offline agent", output)
            self.assertIn(b"all records", output)
            os.write(master, b"a")
            drain(.2)
            os.write(master, b"\x1bOB\n")  # Down, then details.
            drain(.2)
            os.write(master, b"j\x1b/agent\npr")
            drain(.2)
            os.write(master, b"\x1bOC")  # Services.
            drain(.2)
            os.write(master, b"\x1bOD")  # Left: back to Agents.
            drain(.2)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 45, 0, 0))
            child.send_signal(signal.SIGWINCH)
            drain(.2)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 120, 0, 0))
            child.send_signal(signal.SIGWINCH)
            drain(.2)
            os.write(master, b"q")
            drain(.2)
            child.wait(timeout=6)
            self.assertEqual(child.returncode, 0, output.decode(errors="replace"))
            self.assertIn(b"read-only", output)
            self.assertIn(b"Fake agent", output)
            self.assertIn(b"CTM daemon", output)
            self.assertIn(b"Terminal too small", output)
            self.assertNotIn(b"Traceback", output)
            self.assertNotIn(b"\x1b[40m", output)  # NO_COLOR must still preserve the shell background.
            self.assertEqual(self.server.writes, [])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            os.close(master)

    def test_palette_preserves_default_background_and_supports_monochrome(self):
        try:
            import curses
        except ImportError:
            self.skipTest("curses unavailable")
        with patch.dict(os.environ, {}, clear=True), patch.object(curses, "has_colors", return_value=True), \
             patch.object(curses, "start_color"), patch.object(curses, "use_default_colors") as defaults, \
             patch.object(curses, "init_pair") as pairs, patch.object(curses, "color_pair", return_value=0):
            terminal_palette(curses)
            defaults.assert_called_once()
            self.assertEqual(pairs.call_count, 5)
            self.assertTrue(all(call.args[2] == -1 for call in pairs.call_args_list))
        with patch.dict(os.environ, {"NO_COLOR": ""}), patch.object(curses, "has_colors", return_value=True), \
             patch.object(curses, "start_color"), patch.object(curses, "use_default_colors") as defaults, \
             patch.object(curses, "init_pair") as pairs:
            styles = terminal_palette(curses)
            defaults.assert_called_once()
            pairs.assert_not_called()
            self.assertTrue(all(not attr & curses.A_REVERSE for attr in styles.values()))
        with patch.dict(os.environ, {}, clear=True), patch.object(curses, "has_colors", return_value=True), \
             patch.object(curses, "start_color"), patch.object(curses, "use_default_colors", side_effect=curses.error):
            styles = terminal_palette(curses)
            self.assertTrue(all(attr & curses.A_COLOR == 0 for attr in styles.values()))
        self.assertEqual(status_role("delivery_ambiguous"), "error")
        self.assertEqual(status_role("paused"), "warning")
        self.assertEqual(status_role("checking"), "busy")
        self.assertEqual(status_role("online"), "good")


class AgentVisibilityTests(unittest.TestCase):
    def test_main_view_preserves_unnamed_idle_remote_and_unverified_sessions(self):
        rows = [
            {"peer_id": "unnamed", "status": "online", "turn_state": "idle", "addressable": True, "session_lookup": "matched", "last_seen": "2000-01-01T00:00:00Z"},
            {"peer_id": "remote", "status": "online", "session_lookup": "unmatched_remote"},
            {"peer_id": "unknown", "status": "online", "session_lookup": "unknown"},
            {"peer_id": "active", "status": "busy", "session_lookup": "missing_local"},
            {"peer_id": "pending", "status": "online", "pending_ask_count": 1, "session_lookup": "missing_local"},
            {"peer_id": "child", "status": "online", "parent_runtime_id": "parent", "addressable": False},
            {"peer_id": "internal", "status": "online", "ephemeral": True},
            {"peer_id": "offline", "status": "offline"},
            {"peer_id": "archived", "status": "online", "session_archived": True},
            {"peer_id": "missing", "status": "online", "session_lookup": "missing_local"},
            {"peer_id": "denied", "status": "online", "addressable": False},
        ]
        snapshot = {"peers": {"data": rows}}
        self.assertEqual({r["peer_id"] for r in dashboard.rows_for(snapshot, "Agents")}, {"unnamed", "remote", "unknown", "active", "pending"})
        self.assertEqual(len(dashboard.rows_for(snapshot, "Agents", show_all=True)), len(rows))
        self.assertEqual(dashboard.agent_state(rows[5]), "subagent")
        self.assertEqual(dashboard.agent_state(rows[9]), "unmatched")
        self.assertEqual(dashboard.rows_for(snapshot, "Agents", "child"), [])
        self.assertEqual(len(dashboard.rows_for(snapshot, "Agents", "child", show_all=True)), 1)

    def test_local_missing_is_distinct_from_database_unavailable_and_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {"peers": [
                {"peer_id": "local", "backend": "codex", "machine": "here", "status": "online", "metadata": {"runtime_session_id": "missing"}},
                {"peer_id": "remote", "backend": "codex", "machine": "elsewhere", "status": "online", "metadata": {"runtime_session_id": "missing"}},
                {"peer_id": "unnamed", "backend": "codex", "machine": "here", "status": "online", "metadata": {"runtime_session_id": "present"}},
                {"peer_id": "archived", "backend": "codex", "machine": "here", "status": "online", "metadata": {"runtime_session_id": "archived"}},
            ]}
            with patch.dict(os.environ, {"CODEX_HOME": directory}), patch.object(dashboard.socket, "gethostname", return_value="here"):
                unavailable = dashboard.project_peers(raw)
                self.assertEqual(unavailable[0]["session_lookup"], "unknown")
                self.assertEqual(len(dashboard.rows_for({"peers": {"data": unavailable}}, "Agents")), 4)
                with closing(sqlite3.connect(root/"state_5.sqlite")) as conn, conn:
                    conn.execute("CREATE TABLE threads (id TEXT, name TEXT, archived INTEGER)")
                    conn.executemany("INSERT INTO threads VALUES (?, ?, ?)", [("present", None, 0), ("archived", "Old", 1)])
                peers = dashboard.project_peers(raw)
            self.assertEqual(peers[0]["session_lookup"], "missing_local")
            self.assertEqual(peers[0]["visibility_reasons"], ["unmatched"])
            self.assertEqual(peers[1]["session_lookup"], "unmatched_remote")
            self.assertEqual(peers[2]["visibility_reasons"], [])
            self.assertEqual(peers[3]["visibility_reasons"], ["archived"])


class ProcessDetectionTests(unittest.TestCase):
    def test_process_probe_does_not_treat_shell_text_as_a_running_service(self):
        output = subprocess.CompletedProcess([], 0, stdout="1 01:00 repowire serve\n2 02:00 repowire codex-bridge\n3 03:00 codex app-server --listen unix:///tmp/socket\n4 00:01 bash -lc 'repowire codex-bridge'\n", stderr="")
        with patch.object(dashboard.subprocess, "run", return_value=output):
            result = dashboard.processes(2)
        self.assertEqual([p["pid"] for p in result["bridge"]], [2])
        self.assertEqual([p["pid"] for p in result["app_server"]], [3])


if __name__ == "__main__":
    unittest.main()
