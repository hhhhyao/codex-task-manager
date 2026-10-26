"""Read-only snapshots and terminal-independent dashboard views."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import subprocess
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .client import request
from .common import stamp
from .overview import recurring_jobs, schedules, source_errors, task_overview, visible_tasks, when
from .transport import NoRedirect


TASK_FIELDS = ("name", "description", "status", "workload_status", "mode", "next_action", "next_check_at", "next_review_at",
               "buffered", "sending_events", "review_interval_seconds", "review_count", "last_trigger_at", "last_sent_at",
               "id", "owner", "target", "source_done", "interval_seconds", "created_at", "next_flush_at", "last_review_at",
               "last_check_at", "last_successful_check_at",
               "consecutive_errors", "last_error", "check_pid", "cwd", "delivery")
PEER_FIELDS = ("peer_id", "display_name", "name", "backend", "model", "circle", "status", "turn_state",
               "source", "addressable", "ws_connected", "inbound_status", "pending_ask_count",
               "last_seen", "path", "description", "parent_peer_id", "parent_runtime_id", "tmux_session", "session_name",
               "machine", "ephemeral", "addressable_reason")
VIEWS = ("Tasks", "Agents", "Services")


def clean(value):
    text = str(value if value is not None else "-")
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf"})


def clip(value, width):
    result, used = [], 0
    for char in clean(value).replace("\n", " ").replace("\t", " "):
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def pad(value, width):
    value = clip(value, width)
    size = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in value)
    return value + " " * max(0, width - size)


def section(fn):
    try:
        return {"data": fn(), "error": None}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {"data": None, "error": f"HTTP {exc.code}"}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        # Do not echo raw config, URLs, response bodies or environment values.
        return {"data": None, "error": f"{type(exc).__name__}: source unavailable"}


def repowire_connection(timeout):
    base = os.environ.get("CODEX_TASK_MANAGER_REPOWIRE_URL")
    token = os.environ.get("REPOWIRE_DAEMON__AUTH_TOKEN", os.environ.get("REPOWIRE_AUTH_TOKEN"))
    cfg = {}
    if not base or token is None:
        command = [os.environ.get("REPOWIRE_BIN", "repowire"), "config", "get", "daemon"]
        output = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if output.returncode:
            raise ValueError("configuration unavailable")
        cfg = json.loads(output.stdout)
        if not isinstance(cfg, dict):
            raise ValueError("unexpected configuration")
    if not base:
        host = os.environ.get("REPOWIRE_DAEMON__HOST", cfg.get("host", "127.0.0.1"))
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        port = int(os.environ.get("REPOWIRE_DAEMON__PORT", cfg.get("port", 8377)))
        if not 0 < port < 65536:
            raise ValueError("invalid port")
        base = f"http://{host}:{port}"
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid endpoint")
    token = cfg.get("auth_token", "") if token is None else token
    if not isinstance(token, str):
        raise ValueError("invalid token")
    return base.rstrip("/"), token


def read_json(base, token, path, timeout):
    headers = {"Authorization": "Bearer " + token} if token else {}
    req = urllib.request.Request(base + path, headers=headers, method="GET")
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
        body = response.read(1048577)
    if len(body) > 1048576:
        raise ValueError("oversized response")
    return json.loads(body)


def project_rows(value, fields, key=None):
    rows = value[key] if key is not None else value
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("unexpected list response")
    return [{name: row[name] for name in fields if name in row} for row in rows]


def codex_sessions(thread_ids):
    """Optional local enrichment. Never create a DB or read conversation content."""
    if not thread_ids:
        return {}
    root = Path(os.environ.get("CODEX_HOME") or Path.home()/".codex").expanduser()
    try:
        paths = [p for p in root.glob("state_*.sqlite") if p.stem[6:].isdigit()]
        if not paths:
            return None
        path = max(paths, key=lambda p: int(p.stem[6:])).resolve()
        sessions = dict.fromkeys(thread_ids)
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=.1)) as conn:
            fields = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
            if "id" not in fields:
                return None
            columns = ["id", "name" if "name" in fields else "NULL", "archived" if "archived" in fields else "NULL"]
            ids = sorted(thread_ids)
            for start in range(0, len(ids), 500):
                batch = ids[start:start+500]
                rows = conn.execute("SELECT "+", ".join(columns)+" FROM threads WHERE id IN ("+",".join("?" for _ in batch)+")", batch)
                for key, name, archived in rows:
                    sessions[key] = {"name": name.strip() if isinstance(name, str) else None, "archived": bool(archived)}
        return sessions
    except (OSError, ValueError, sqlite3.Error):
        return None


def agent_visibility_reasons(row):
    """Inventory is preserved; only the main-session view is narrowed."""
    reasons = []
    if row.get("status") == "offline":
        reasons.append("offline")
    if row.get("parent_peer_id") or row.get("parent_runtime_id") or row.get("thread_source") == "subagent" or str(row.get("addressable_reason", "")).startswith("subagent_"):
        reasons.append("subagent")
    if row.get("ephemeral") is True or row.get("thread_source") == "system":
        reasons.append("internal")
    if row.get("addressable") is False:
        reasons.append("no input")
    if row.get("session_archived"):
        reasons.append("archived")
    busy = row.get("turn_state") in {"working", "active"} or row.get("status") == "busy"
    if row.get("session_lookup") == "missing_local" and not busy and not row.get("pending_ask_count"):
        reasons.append("unmatched")
    return reasons


def agent_state(row):
    return next(iter(agent_visibility_reasons(row)), row.get("status"))


def project_peers(value):
    peers = project_rows(value, PEER_FIELDS, "peers")
    for row, raw in zip(peers, value["peers"]):
        metadata = raw.get("metadata")
        if row.get("backend") != "codex" or not isinstance(metadata, dict):
            continue
        thread_id = metadata.get("runtime_session_id") or metadata.get("hook_session_id")
        if isinstance(thread_id, str) and thread_id:
            row["session_id"] = thread_id
        nickname = metadata.get("agent_nickname")
        if isinstance(nickname, str) and nickname:
            row["agent_nickname"] = nickname
        source = metadata.get("codex_thread_source")
        if isinstance(source, str):
            row["thread_source"] = source
    sessions = codex_sessions({p["session_id"] for p in peers if p.get("session_id")})
    hostname = socket.gethostname()
    local_names = {hostname, hostname.split(".")[0]}
    for row in peers:
        session_id = row.get("session_id")
        if session_id:
            session = sessions.get(session_id) if sessions is not None else None
            row["session_lookup"] = ("unknown" if sessions is None else "matched" if session else
                                     "missing_local" if row.get("machine") in local_names else "unmatched_remote")
            if session:
                if not row.get("session_name") and session["name"]:
                    row["session_name"] = session["name"]
                row["session_archived"] = session["archived"]
        row["visibility_reasons"] = agent_visibility_reasons(row)
    return peers


def processes(timeout):
    output = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "pid=,etime=,args="],
                            capture_output=True, text=True, timeout=timeout)
    if output.returncode:
        raise ValueError("process inspection unavailable")
    found = {"repowire": [], "bridge": [], "app_server": []}
    for line in output.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            argv = shlex.split(fields[2])
        except ValueError:
            continue
        if not argv:
            continue
        exe = Path(argv[0]).name
        key = ("bridge" if exe == "repowire" and "codex-bridge" in argv[1:] else
               "repowire" if exe == "repowire" and "serve" in argv[1:] else
               "app_server" if exe in {"codex", "codex-app-server"} and "--listen" in argv[1:] else None)
        if key:
            found[key].append({"pid": int(fields[0]), "elapsed": fields[1]})
    return found


def collect_snapshot(directory, timeout=2):
    """Independent failures stay visible; nothing starts or changes a service."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        pending = {
            "ctm": pool.submit(section, lambda: request(directory, "ping", timeout=timeout)),
            "tasks": pool.submit(section, lambda: project_rows(request(directory, "list", timeout=timeout), TASK_FIELDS)),
            "processes": pool.submit(section, lambda: processes(timeout)),
            "connection": pool.submit(section, lambda: repowire_connection(timeout)),
        }
        connection = pending.pop("connection").result()
        endpoint = None
        if connection["error"]:
            health = peers = schedule_rows = jobs = {"data": None, "error": connection["error"]}
        else:
            base, token = connection["data"]
            endpoint = base
            def health_data():
                value = read_json(base, token, "/health", timeout)
                if not isinstance(value, dict) or "status" not in value:
                    raise ValueError("unexpected health response")
                return {k: value[k] for k in ("status", "peers", "schema_version") if k in value}
            health_future = pool.submit(section, health_data)
            peers_future = pool.submit(section, lambda: project_peers(read_json(base, token, "/peers?include_hidden=true", timeout)))
            schedules_future = pool.submit(section, lambda: schedules(read_json(base, token, "/schedules", timeout)))
            jobs_future = pool.submit(section, lambda: recurring_jobs(read_json(base, token, "/work?view=summary", timeout)))
            health, peers = health_future.result(), peers_future.result()
            schedule_rows, jobs = schedules_future.result(), jobs_future.result()
        result = {key: future.result() for key, future in pending.items()}
    result.update(updated_at=stamp(), state_dir=str(directory), repowire=health, peers=peers,
                  repowire_endpoint=endpoint, schedules=schedule_rows, recurring_jobs=jobs)
    result["task_overview"] = task_overview(result)
    proc = result["processes"]["data"]
    process_rows = proc or {}
    ping = result["ctm"]["data"]
    health = result["repowire"]["data"]
    result["services"] = [
        {"name": "CTM daemon", "status": "reachable" if ping else "unavailable",
         "pids": [ping["pid"]] if ping else [], "evidence": "CTM control socket", "error": result["ctm"]["error"],
         "state_dir": str(directory), "active_tasks": ping.get("active_tasks") if ping else None,
         "checks_running": ping.get("checks_running") if ping else None, "sends_running": ping.get("sends_running") if ping else None},
        {"name": "Repowire daemon", "status": "reachable" if health and health.get("status") == "ok" else "unavailable",
         "pids": [p["pid"] for p in process_rows.get("repowire", [])], "evidence": "HTTP /health + local process", "error": result["repowire"]["error"], "endpoint": endpoint},
    ]
    for key, label in (("bridge", "Codex bridge"), ("app_server", "Codex App Server")):
        result["services"].append({"name": label, "status": "running" if process_rows.get(key) else "not observed" if proc is not None else "unknown",
            "pids": [p["pid"] for p in process_rows.get(key, [])], "evidence": "local process only",
            "elapsed": [p.get("elapsed", "unknown") for p in process_rows.get(key, [])], "error": result["processes"]["error"]})
    return result


def rows_for(snapshot, view, query="", show_all=False, source="all"):
    if view == "Services":
        rows = snapshot.get("services", [])
    elif view == "Tasks":
        rows = visible_tasks(snapshot, source, show_all)
    else:
        rows = snapshot.get("peers", {}).get("data") or []
    if view == "Agents" and not show_all:
        rows = [r for r in rows if not agent_visibility_reasons(r)]
    if view == "Agents":
        rows = sorted(rows, key=lambda r: str((r.get("session_name") if view == "Agents" else None) or r.get("name") or r.get("display_name") or r.get("id") or "").lower())
    if query:
        rows = [r for r in rows if query.casefold() in json.dumps(r, ensure_ascii=False).casefold()]
    return rows


def row_id(row):
    return row.get("view_id") or row.get("id") or row.get("peer_id") or row.get("name")


def table_layout(snapshot, view, rows, width):
    peer_names = {r.get("peer_id"): r.get("display_name") or r.get("name") for r in snapshot.get("peers", {}).get("data") or []}
    if view == "Services":
        headers, sizes = ["SERVICE", "STATE", "PID"], [max(15, width-32), 13, 15]
        cells = [[r["name"], r["status"], ",".join(map(str, r["pids"])) or "-"] for r in rows]
        if width >= 100:
            headers, sizes = ["SERVICE", "STATE", "PID", "EVIDENCE"], [21, 13, 19, width-59]
            cells = [values+[r["evidence"]] for values, r in zip(cells, rows)]
    elif view == "Tasks":
        if width < 100:
            headers, sizes = ["SOURCE", "TASK", "STATE", "NEXT UTC"], [11, max(9, width-44), 11, 16]
        elif width < 160:
            headers, sizes = ["SOURCE", "TASK", "STATE", "DESCRIPTION", "NEXT UTC", "TARGET"], [11, 20, 14, max(10, width-83), 16, 12]
        else:
            headers, sizes = ["SOURCE", "TASK", "STATE", "DESCRIPTION", "MODE", "NEXT UTC", "OWNER", "TARGET", "BUF", "REVIEW UTC"], [11, 22, 17, width-137, 6, 16, 14, 14, 3, 16]
        cells = []
        for row in rows:
            owner = row.get("owner") or {}
            owner = owner if isinstance(owner, dict) else {}
            owner = owner.get("label") or peer_names.get(owner.get("peer_id")) or owner.get("thread_id", "-")[:8]
            target = row.get("target_name")
            if row.get("target_state") in {"offline", "no input", "disconnected"}:
                target = f"[{row['target_state']}] {target}"
            values = {"SOURCE": row.get("source", "CTM"), "TASK": row.get("name"), "STATE": row.get("status"), "DESCRIPTION": row.get("description"),
                      "OWNER": row.get("owner_name") or owner, "MODE": row.get("mode"), "BUF": row.get("buffered"),
                      "TARGET": target, "NEXT UTC": when(row.get("next_at") or row.get("next_check_at") or row.get("next_review_at")),
                      "REVIEW UTC": when(row.get("next_review_at"))}
            cells.append([values[header] for header in headers])
    else:
        compact = width < 90
        headers, sizes = (["SESSION", "TURN", "STATE"], [max(12, width-28), 9, 15]) if compact else (
            ["SESSION", "AGENT", "STATE", "TURN", "INBOUND", "ASKS"], [max(12, width-73), 24, 13, 9, 13, 4])
        cells = [[r.get("session_name") or r.get("display_name") or r.get("name"), r.get("turn_state"), agent_state(r)] if compact else
                 [r.get("session_name"), r.get("display_name") or r.get("name"), agent_state(r), r.get("turn_state"), r.get("inbound_status"), r.get("pending_ask_count", 0)] for r in rows]
    return headers, sizes, cells


def table_lines(snapshot, view, rows, width):
    headers, sizes, cells = table_layout(snapshot, view, rows, width)
    return [clip("  ".join(pad(value, size) for value, size in zip(values, sizes)), width) for values in [headers, *cells]]


def text_snapshot(snapshot, width=120):
    lines = [f"Codex Task Manager | read-only | {snapshot['updated_at']} (UTC)", f"State: {snapshot['state_dir']}"]
    for view in VIEWS:
        lines.extend(["", view])
        source = snapshot.get("tasks" if view == "Tasks" else "peers", {})
        if view == "Tasks":
            if snapshot.get("repowire_endpoint"):
                lines.append("Repowire: " + snapshot["repowire_endpoint"])
            lines.extend("Unavailable: " + error for error in source_errors(snapshot))
            rows = rows_for(snapshot, view)
            lines.extend(table_lines(snapshot, view, rows, width) if rows else ["No entries from available sources."])
        elif view != "Services" and source.get("error"):
            lines.append("Unavailable: " + source["error"])
        else:
            rows = rows_for(snapshot, view)
            if view == "Agents":
                lines.append(f"Main sessions: {len(rows)}; other records hidden: {len(source.get('data') or [])-len(rows)} (ctm ui: a shows all)")
            lines.extend(table_lines(snapshot, view, rows, width) if rows else ["No entries."])
    return "\n".join(lines)
