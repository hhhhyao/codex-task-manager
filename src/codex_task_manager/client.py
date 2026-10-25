import json
import socket

from .common import socket_path


def request(state_dir, operation, **values):
    data = json.dumps({"op": operation, **values}, ensure_ascii=False).encode() + b"\n"
    if len(data) > 262144:
        raise ValueError("request exceeds 256 KiB; submit brief events")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        try:
            connection.connect(str(socket_path(state_dir)))
        except OSError as exc:
            raise ValueError("daemon unavailable; use 'ctm daemon start' with the same --state-dir") from exc
        connection.sendall(data)
        with connection.makefile("rb") as stream:
            reply = stream.readline(16777217)
    if len(reply) > 16777216:
        raise ValueError("response too large; query individual tasks")
    result = json.loads(reply)
    if not result.get("ok"):
        raise ValueError(result.get("error", "daemon request failed"))
    return result["result"]
