"""Repowire HTTP transport, with no successful-message archive."""

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .common import stamp


def config(key, default):
    result = subprocess.run([os.environ.get("REPOWIRE_BIN", "repowire"), "config", "get", key],
                            capture_output=True, text=True, timeout=5)
    return json.loads(result.stdout) if result.returncode == 0 else default


def connection():
    base = os.environ.get("CODEX_TASK_MANAGER_REPOWIRE_URL")
    if not base:
        host = os.environ.get("REPOWIRE_DAEMON__HOST") or config("daemon.host", "127.0.0.1")
        port = os.environ.get("REPOWIRE_DAEMON__PORT") or config("daemon.port", 8377)
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        base = f"http://{host}:{int(port)}"
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid Repowire URL")
    token = os.environ.get("REPOWIRE_DAEMON__AUTH_TOKEN")
    if token is None:
        token = os.environ.get("REPOWIRE_AUTH_TOKEN")
    if token is None:
        token = config("daemon.auth_token", "")
    if not isinstance(token, str):
        raise ValueError("invalid Repowire token")
    return base.rstrip("/") + "/notify", token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def classify(response):
    if not isinstance(response, dict) or not isinstance(response.get("hook_delivery", {}), dict):
        return "ambiguous"
    hook = response.get("hook_delivery", {}).get("status")
    if hook is not None and not isinstance(hook, str):
        return "ambiguous"
    if response.get("ok") is False or hook in {"failed", "rejected"}:
        return "rejected"
    if response.get("queued") is True or response.get("delivery_state") == "queued":
        return "queued"
    return "accepted" if hook in {"accepted", "injected"} else "ambiguous"


def send(spec, batch):
    # Config is resolved afresh: routine token/endpoint changes need no task rewrite.
    try:
        url, token = connection()
        text = f"[codex-task-manager: {spec['id']}/{batch['id']}]\n[sent_at: {stamp()}]\n{batch['text']}"
        payload = {"from_peer": spec["owner"]["peer_id"], "to_peer": spec["target"]["peer_id"], "text": text}
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, json.dumps(payload).encode(), headers, method="POST")
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return "rejected", "local Repowire configuration unavailable; inspect daemon environment/config"
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            data = response.read(65537)
            if len(data) > 65536:
                return "ambiguous", "oversized transport response"
            status = classify(json.loads(data))
        return status, None if status in {"accepted", "queued"} else "Repowire returned rejection or no verifiable receipt"
    except urllib.error.HTTPError as exc:
        status = "ambiguous" if exc.code >= 500 or exc.code == 408 else "rejected"
        return status, f"HTTP {exc.code}"
    except (OSError, ValueError, urllib.error.URLError):
        return "ambiguous", "response lost/unreadable; verify actual delivery before retry"
