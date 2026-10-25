"""Log in through the running shared Codex App Server without restarting it."""

import argparse
import asyncio
from contextlib import suppress
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, parse_qs

from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed

from . import __version__


class AccountError(RuntimeError):
    pass


def say(message):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {message}", flush=True)


class Client:
    def __init__(self, socket):
        self.socket = socket
        self.next_id = 0
        self.pending = {}
        self.completions = asyncio.Queue()

    async def __aenter__(self):
        self.ws = await unix_connect(str(self.socket), uri="ws://localhost/",
                                     open_timeout=5, close_timeout=3, max_size=64 * 1024 * 1024)
        self.reader = asyncio.create_task(self.read())
        try:
            await self.request("initialize", {
                "clientInfo": {"name": "codex-server", "title": "Codex Server Login", "version": __version__},
                "capabilities": {"experimentalApi": True},
            })
            await self.ws.send(json.dumps({"method": "initialized", "params": {}}))
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        await self.ws.close()
        self.reader.cancel()
        with suppress(asyncio.CancelledError):
            await self.reader

    async def read(self):
        failure = AccountError("App Server 连接已关闭；请检查服务状态。")
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                if "id" in data and ("result" in data or "error" in data):
                    future = self.pending.get(data["id"])
                    if future and not future.done():
                        if "error" in data:
                            error = data["error"]
                            future.set_exception(AccountError(
                                f"App Server 请求失败：{error.get('message', 'unknown error')}"))
                        else:
                            future.set_result(data["result"])
                elif data.get("method") == "account/login/completed":
                    self.completions.put_nowait(data.get("params", {}))
                elif "id" in data and "method" in data:
                    await self.ws.send(json.dumps({"id": data["id"], "error": {
                        "code": -32601, "message": "Account client does not handle this server request"}}))
        except Exception:
            failure = AccountError("App Server 连接中断；请重新查询账号，避免重复发起登录。")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            self.completions.put_nowait({"connection_error": True})

    async def request(self, method, params):
        self.next_id += 1
        rid = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.ws.send(json.dumps({"id": rid, "method": method, "params": params}))
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(rid, None)

    async def wait_login(self, login_id, timeout):
        async with asyncio.timeout(timeout):
            while True:
                event = await self.completions.get()
                if event.get("connection_error"):
                    raise AccountError("登录期间连接中断；请先运行 status 核对实际状态。")
                if event.get("loginId") == login_id:
                    if event.get("success") is not True:
                        raise AccountError("登录未成功：" + str(event.get("error") or "cancelled"))
                    return


def account_summary(result, redact=False):
    account = result.get("account") or {}
    email = account.get("email")
    if redact and email:
        email = "<hidden>"
    workspace = result.get("workspaceRouting") or {}
    return {"type": account.get("type"), "email": email, "plan": account.get("planType"),
            "workspace_id": "<hidden>" if redact and workspace.get("chatgptAccountId")
                            else workspace.get("chatgptAccountId"),
            "requires_openai_auth": result.get("requiresOpenaiAuth")}


def identity(result):
    account = result.get("account") or {}
    workspace = result.get("workspaceRouting") or {}
    return account.get("type"), account.get("email"), workspace.get("chatgptAccountId")


async def execute(args):
    async with Client(args.socket) as client:
        before = await client.request("account/read", {"refreshToken": False})
        if args.command == "status":
            summary = account_summary(before, args.redact)
            if args.json:
                print(json.dumps(summary, ensure_ascii=False))
            else:
                say("当前 shared App Server 账号：" + json.dumps(summary, ensure_ascii=False))
            return
        say("在现有 shared App Server 中登录；完成后该服务下的所有会话共用新账号。")
        say("当前账号：" + json.dumps(account_summary(before), ensure_ascii=False))
        login_id = None
        completed = False
        try:
            mode = "chatgpt" if args.browser else "chatgptDeviceCode"
            result = await client.request("account/login/start", {"type": mode})
            login_id = result.get("loginId")
            if not login_id:
                raise AccountError("服务未返回 loginId；请运行 status 核对状态。")
            if args.browser:
                auth_url = result.get("authUrl")
                if not auth_url:
                    raise AccountError("服务未返回登录网址。")
                say("在浏览器中打开以下网址，选择目标账号（不要将链接发给他人）：")
                print(auth_url, flush=True)
                callback = parse_qs(urlsplit(auth_url).query).get("redirect_uri", [None])[0]
                if callback:
                    port = urlsplit(callback).port
                    if port:
                        say(f"通过 SSH 使用时，浏览器所在电脑需转发本地端口 {port} 到此服务器的 {port}。")
            else:
                say("打开以下网址，使用目标账号登录并输入一次性验证码：")
                url, code = result.get("verificationUrl"), result.get("userCode")
                if not isinstance(url, str) or not url or not isinstance(code, str) or not code:
                    raise AccountError("服务未返回有效的 device auth 网址和验证码。")
                print(url, flush=True)
                print("Code: " + code, flush=True)
            say("等待登录完成；Ctrl+C 只取消本次登录，不退出当前账号。")
            await client.wait_login(login_id, args.timeout)
            completed = True
            after = await client.request("account/read", {"refreshToken": False})
            if not after.get("account"):
                raise AccountError("已收到登录完成消息，但账号读回为空，请重新查询 status。")
            say("服务端账号已读回：" + json.dumps(account_summary(after), ensure_ascii=False))
            if identity(before) == identity(after):
                say("登录成功，但可见账号身份未改变；如需另一个账号，请在浏览器中选择它。")
            else:
                say("账号身份已更新；未重启 App Server 或 Repowire。")
            say("已在途请求的账号归属尚未验证；后续推理可通过 Codex /status 与实际请求核对。")
        finally:
            if login_id and not completed:
                with suppress(Exception):
                    await client.request("account/login/cancel", {"loginId": login_id})


def main(argv=None):
    root = Path(os.environ.get("CODEX_HOME") or Path.home()/".codex").expanduser()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--socket", type=Path,
                        default=root / "app-server-control/app-server-control.sock")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Read the running server's account; no refresh or login")
    status.add_argument("--json", action="store_true")
    status.add_argument("--redact", action="store_true", help="Hide email and workspace identifier")
    login = commands.add_parser("login", help="Log in through the running server; affects its shared sessions")
    modes = login.add_mutually_exclusive_group()
    modes.add_argument("--device", action="store_true", help="Device-code login (default; suited to SSH)")
    modes.add_argument("--browser", action="store_true", help="Browser login; remote callback needs port forwarding")
    login.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    args.socket = args.socket.expanduser()
    if not args.socket.is_socket():
        parser.error("shared App Server socket 不存在；先运行 codex app-server daemon start")
    if args.command == "login" and (not math.isfinite(args.timeout) or not 0 < args.timeout <= 1800):
        parser.error("timeout 必须在 (0, 1800] 秒范围内")
    try:
        if args.command == "login":
            # Coordinate with the previously installed codex-account helper, too.
            lock_path = args.socket.parent / "codex-account-login.lock"
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise AccountError("另一个 codex-server 登录仍在进行。") from None
                asyncio.run(execute(args))
        else:
            asyncio.run(execute(args))
    except KeyboardInterrupt:
        say("已结束本次登录等待；可运行 codex-server status 核对当前账号。")
        return 130
    except (AccountError, TimeoutError, OSError, ConnectionClosed) as error:
        say(str(error) if str(error) else "请求超时；请先运行 status 核对实际状态。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
