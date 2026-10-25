import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import uuid


def stamp(epoch=None):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat() if epoch is not None else dt.datetime.now(dt.timezone.utc).isoformat()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("time must be an ISO timestamp with timezone")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("time must include timezone")
    return parsed.timestamp()


def number(value, name, minimum=0, maximum=86400 * 365):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be a finite number in [{minimum}, {maximum}]")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def state_directory(value=None):
    return Path(value or os.environ.get("CODEX_TASK_MANAGER_HOME") or
                Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "codex-task-manager").expanduser().resolve()


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError(f"directory is not privately owned: {path}")
    path.chmod(0o700)


def socket_path(state_dir):
    # Home directories on shared filesystems often exceed sockaddr_un's limit.
    base = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"codex-task-manager-{os.getuid()}"
    return base / hashlib.sha256(str(state_dir).encode()).hexdigest()[:16] / "control.sock"


def identity(value):
    if not isinstance(value, dict) or not isinstance(value.get("peer_id"), str) or not value["peer_id"] or not isinstance(value.get("thread_id"), str):
        raise ValueError("owner/target requires exact peer_id and thread_id")
    uuid.UUID(value.get("thread_id", ""))
    if any(not isinstance(v, str) or len(v) > 200 for v in value.values()):
        raise ValueError("identity values must be short strings")
    return {k: value[k] for k in ("peer_id", "thread_id", "label") if k in value}


def validate_description(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
        raise ValueError("description must contain 1..1000 characters")
    return value.strip()


def validate_spec(raw):
    if not isinstance(raw, dict):
        raise ValueError("task specification must be an object")
    allowed = {"id", "name", "owner", "target", "cwd", "check", "interval_seconds", "fire_at",
               "mode", "window_seconds", "review_interval_seconds", "check_timeout", "max_events",
               "max_buffer_bytes", "max_message_chars", "seen_limit", "retry_seconds",
               "max_attempts", "error_notify_after", "plan_path", "message", "next_action", "description"}
    if set(raw) - allowed:
        raise ValueError(f"unknown task fields: {sorted(set(raw) - allowed)}")
    spec = dict(raw)
    spec["id"] = str(uuid.UUID(spec["id"])) if "id" in spec else str(uuid.uuid4())
    spec["owner"] = identity(spec.get("owner"))
    spec["target"] = identity(spec.get("target", spec["owner"]))
    if not isinstance(spec.get("name"), str) or not 1 <= len(spec["name"]) <= 160:
        raise ValueError("name must contain 1..160 characters")
    if "description" in spec:
        spec["description"] = validate_description(spec["description"])
    if "message" in spec and (not isinstance(spec["message"], str) or not 1 <= len(spec["message"].strip()) <= 2000):
        raise ValueError("timer message must contain 1..2000 characters")
    if "next_action" in spec and (not isinstance(spec["next_action"], str) or not 1 <= len(spec["next_action"].strip()) <= 1000):
        raise ValueError("next_action must contain 1..1000 characters")
    spec.setdefault("mode", "once")
    if spec["mode"] not in {"once", "repeat"}:
        raise ValueError("mode must be once or repeat")
    spec["cwd"] = str(Path(spec.get("cwd", ".")).expanduser().resolve())
    if not Path(spec["cwd"]).is_dir():
        raise ValueError("cwd must be an existing directory")
    if "check" in spec:
        command = spec["check"]
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x or "\0" in x for x in command):
            raise ValueError("check must be a nonempty argv array")
        spec.setdefault("interval_seconds", 60)
    for field in ("fire_at",):
        if field in spec:
            timestamp(spec[field])
    if "fire_at" in spec and "check" in spec:
        raise ValueError("fire_at is a timer, not a delayed check; use review_interval_seconds for pending checks")
    if spec["mode"] == "repeat" and "fire_at" in spec and "interval_seconds" not in spec:
        raise ValueError("a repeating timer requires interval_seconds")
    if "interval_seconds" in spec:
        number(spec["interval_seconds"], "interval_seconds", 0.05)
    if "review_interval_seconds" in spec:
        number(spec["review_interval_seconds"], "review_interval_seconds", 0.05)
    defaults = {"window_seconds": 60, "check_timeout": 15, "max_events": 50,
                "max_buffer_bytes": 131072, "max_message_chars": 6000, "seen_limit": 256,
                "retry_seconds": 10, "max_attempts": 3, "error_notify_after": 3}
    bounds = {"window_seconds": (0, 86400), "check_timeout": (0.05, 300),
              "max_events": (1, 1000), "max_buffer_bytes": (4096, 1048576),
              "max_message_chars": (800, 16000), "seen_limit": (1, 4096),
              "retry_seconds": (0.05, 3600), "max_attempts": (1, 10), "error_notify_after": (1, 100)}
    for key, default in defaults.items():
        spec.setdefault(key, default)
        number(spec[key], key, *bounds[key])
        if key not in {"window_seconds", "check_timeout", "retry_seconds"} and not isinstance(spec[key], int):
            raise ValueError(f"{key} must be an integer")
    if spec["seen_limit"] < spec["max_events"]:
        raise ValueError("seen_limit must be at least max_events")
    if "plan_path" in spec:
        if not isinstance(spec["plan_path"], str) or not Path(spec["plan_path"]).is_absolute():
            raise ValueError("plan_path must be absolute")
    return spec
