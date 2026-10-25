"""Local fake App Server tests; never log in to a real account."""

import argparse
import asyncio
from contextlib import redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from websockets.asyncio.server import unix_serve

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from codex_task_manager import server as app


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket = Path(self.tmp.name) / "s.sock"
        self.calls = []
        self.account = "old@example.test"
        self.mode = "success"
        self.server = await unix_serve(self.handler, str(self.socket))

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.tmp.cleanup()

    async def handler(self, ws):
        async for raw in ws:
            data = json.loads(raw)
            method = data.get("method")
            if method is None:
                continue
            self.calls.append((method, data.get("params")))
            if "id" not in data:
                continue
            if method == "initialize":
                result = {"userAgent": "fake"}
            elif method == "account/read":
                result = {"account": {"type": "chatgpt", "email": self.account, "planType": "pro"},
                          "requiresOpenaiAuth": True}
            elif method == "account/login/start":
                if self.mode == "rejected":
                    await ws.send(json.dumps({"id": data["id"], "error": {
                        "code": -32600, "message": "device login unavailable"}}))
                    continue
                if self.mode == "disconnect":
                    await ws.close()
                    return
                result = {"type": data["params"]["type"], "loginId": "own-login",
                          "verificationUrl": "https://example.test/device", "userCode": "TEST-CODE",
                          "authUrl": "https://example.test/login?redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fcallback"}
                if self.mode == "missing-code":
                    result.pop("userCode")
                # Completion can race the response to login/start.
                if self.mode not in ("timeout",):
                    await ws.send(json.dumps({"method": "account/login/completed", "params": {
                        "loginId": "someone-elses-login", "success": True}}))
                    await ws.send(json.dumps({"method": "account/login/completed", "params": {
                        "loginId": "own-login", "success": self.mode != "failed", "error": None}}))
                    if self.mode not in ("same", "failed"):
                        self.account = "new@example.test"
            elif method == "account/login/cancel":
                result = {}
            else:
                result = {}
            await ws.send(json.dumps({"id": data["id"], "result": result}))

    def args(self, command="login", browser=False):
        return argparse.Namespace(socket=self.socket, command=command, browser=browser,
                                  timeout=0.1, json=True, redact=True)

    async def test_status_is_read_only_and_redacts_identity(self):
        output = io.StringIO()
        with redirect_stdout(output):
            await app.execute(self.args("status"))
        result = json.loads(output.getvalue())
        self.assertEqual(result["email"], "<hidden>")
        self.assertNotIn("old@example.test", output.getvalue())
        self.assertEqual([p for m, p in self.calls if m == "account/read"], [{"refreshToken": False}])
        self.assertFalse(any(m.startswith("account/login") for m, p in self.calls))

    async def test_device_login_reads_back_new_account_and_handles_early_completion(self):
        output = io.StringIO()
        with redirect_stdout(output):
            await app.execute(self.args())
        self.assertIn("new@example.test", output.getvalue())
        self.assertIn("TEST-CODE", output.getvalue())
        self.assertIn(("account/login/start", {"type": "chatgptDeviceCode"}), self.calls)
        self.assertFalse(any(m == "account/login/cancel" for m, p in self.calls))

    async def test_browser_login_reports_callback_forwarding(self):
        output = io.StringIO()
        with redirect_stdout(output):
            await app.execute(self.args(browser=True))
        self.assertIn(("account/login/start", {"type": "chatgpt"}), self.calls)
        self.assertIn("1455", output.getvalue())

    async def test_same_identity_does_not_claim_an_account_switch(self):
        self.mode = "same"
        output = io.StringIO()
        with redirect_stdout(output):
            await app.execute(self.args())
        self.assertIn("身份未改变", output.getvalue())

    async def test_timeout_cancels_only_own_login_without_logout(self):
        self.mode = "timeout"
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(TimeoutError):
                await app.execute(self.args())
        self.assertIn(("account/login/cancel", {"loginId": "own-login"}), self.calls)
        self.assertFalse(any(m == "account/logout" for m, p in self.calls))

    async def test_failed_login_is_not_reported_as_success(self):
        self.mode = "failed"
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(app.AccountError):
                await app.execute(self.args())
        self.assertEqual(self.account, "old@example.test")

    async def test_disconnect_fails_without_restarting_or_repeating_login(self):
        self.mode = "disconnect"
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(app.AccountError):
                await app.execute(self.args())
        self.assertEqual(sum(m == "account/login/start" for m, p in self.calls), 1)

    async def test_rejected_login_is_not_retried_as_another_auth_mode(self):
        self.mode = "rejected"
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(app.AccountError, "device login unavailable"):
                await app.execute(self.args())
        self.assertEqual(sum(m == "account/login/start" for m, p in self.calls), 1)

    async def test_missing_device_code_is_not_printed_as_valid_login(self):
        self.mode = "missing-code"
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(app.AccountError, "device auth"):
                await app.execute(self.args())
        self.assertIn(("account/login/cancel", {"loginId": "own-login"}), self.calls)

    async def test_cancelled_wait_cancels_only_the_owned_login(self):
        self.mode = "timeout"
        args = self.args()
        args.timeout = 30
        with redirect_stdout(io.StringIO()):
            task = asyncio.create_task(app.execute(args))
            async with asyncio.timeout(2):
                while not any(m == "account/login/start" for m, _ in self.calls):
                    await asyncio.sleep(.01)
            await asyncio.sleep(.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn(("account/login/cancel", {"loginId": "own-login"}), self.calls)
        self.assertFalse(any(m == "account/logout" for m, _ in self.calls))

    async def cli(self, *args, env=None):
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "codex_task_manager.server", *args,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]/"src"), **(env or {})},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(child.communicate(), 8)
        except BaseException:
            child.kill()
            await child.communicate()
            raise
        return child.returncode, out.decode(), err.decode()

    async def test_cli_status_uses_codex_home_and_only_reads_the_account(self):
        control = Path(self.tmp.name)/"app-server-control"
        control.mkdir()
        async with unix_serve(self.handler, str(control/"app-server-control.sock")):
            rc, out, err = await self.cli("status", "--json", "--redact", env={"CODEX_HOME": self.tmp.name})
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["email"], "<hidden>")
        self.assertFalse(any(m.startswith("account/login") for m, _ in self.calls))

    async def test_cli_device_login_and_custom_socket(self):
        rc, out, err = await self.cli("--socket", str(self.socket), "login", "--device")
        self.assertEqual(rc, 0, err)
        self.assertIn("TEST-CODE", out)
        self.assertIn("new@example.test", out)
        self.assertIn(("account/login/start", {"type": "chatgptDeviceCode"}), self.calls)

    async def test_cli_rejects_concurrent_login_before_contacting_the_server(self):
        with (self.socket.parent/"codex-account-login.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc, out, err = await self.cli("--socket", str(self.socket), "login", "--device")
        self.assertEqual(rc, 1, err)
        self.assertIn("另一个", out)
        self.assertEqual(self.calls, [])

    async def test_cli_rejects_invalid_timeouts_and_missing_socket_without_login(self):
        for value in ("nan", "inf", "0", "1801"):
            rc, out, err = await self.cli("--socket", str(self.socket), "login", "--timeout", value)
            self.assertEqual(rc, 2, out+err)
        missing = self.socket.parent/"missing.sock"
        rc, out, err = await self.cli("--socket", str(missing), "status")
        self.assertEqual(rc, 2, out+err)
        self.assertFalse(missing.exists())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
