import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import __version__
from .client import request
from .common import state_directory


def daemon_start(directory):
    try:
        return request(directory, "ping")
    except ValueError:
        pass
    child = subprocess.Popen([sys.executable, "-m", "codex_task_manager", "--state-dir", str(directory), "daemon", "run"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return request(directory, "ping")
        except ValueError:
            if child.poll() is not None:
                # Another concurrent start may have won the daemon lock.
                try:
                    return request(directory, "ping")
                except ValueError:
                    raise ValueError(f"daemon failed to start; inspect {directory / 'daemon.log'}") from None
            time.sleep(0.05)
    raise ValueError(f"daemon startup timed out; inspect {directory / 'daemon.log'}")


def table(rows):
    if not rows:
        return "No active tasks."
    headers = ["ID", "NAME", "OWNER", "MODE", "STATE", "BUFFER", "NEXT CHECK", "REVIEW EVERY", "NEXT REVIEW", "LAST TRIGGER", "NEXT ACTION"]
    data = []
    def short_time(value):
        return value[:19].replace("T", " ") if value else "-"
    for row in rows:
        data.append([row["id"], row["name"][:28], row["owner"].get("label", row["owner"]["thread_id"][:8]),
                     row["mode"], row["status"], str(row["buffered"]), short_time(row["next_check_at"]),
                     f"{row['review_interval_seconds']:g}s" if row.get("review_interval_seconds") is not None else "-",
                     short_time(row["next_review_at"]), short_time(row["last_trigger_at"]),
                     (row.get("next_action") or "-").replace("\n", " ")[:36]])
    widths = [max(len(values[i]) for values in [headers, *data]) for i in range(len(headers))]
    return "Times: UTC\n" + "\n".join("  ".join(value.ljust(width) for value, width in zip(values, widths)) for values in [headers, *data])


def parser():
    root = argparse.ArgumentParser(description="Shared observation tasks and short-lived Repowire notification buffers")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--state-dir", help="Shared state directory; all agents use the same directory")
    root.add_argument("--json", action="store_true")
    commands = root.add_subparsers(dest="command", required=True)
    def command(name, **kwargs):
        item = commands.add_parser(name, **kwargs)
        item.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return item
    daemon = command("daemon", help="Start, inspect or stop the shared local daemon")
    sub = daemon.add_subparsers(dest="action", required=True)
    for action in ("start", "status", "stop", "run"):
        item = sub.add_parser(action)
        item.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if action == "run":
            item.add_argument("--max-checks", type=int, default=4)
            item.add_argument("--max-sends", type=int, default=4)
            item.add_argument("--retention-seconds", type=float, default=300)
    item = command("submit", help="Register a task specification JSON file")
    item.add_argument("file", type=Path)
    item = command("list", help="Current tasks across agents; no event history")
    group = item.add_mutually_exclusive_group()
    group.add_argument("--mine", action="store_true")
    group.add_argument("--owner", help="Exact owner thread UUID")
    group.add_argument("--all", action="store_true", help="All owners (the default)")
    item.add_argument("--include-finished", action="store_true")
    item = command("show")
    item.add_argument("id")
    item.add_argument("--buffer", action="store_true", help="Explicitly include only currently buffered events")
    for name in ("cancel", "flush", "resume"):
        command(name).add_argument("id")
    item = command("emit", help="Submit one event or an array of events from a producer callback")
    item.add_argument("id")
    item.add_argument("file", type=Path)
    item = command("review", help="Set or disable the repeating waiting-review interval (seconds)")
    item.add_argument("id")
    group = item.add_mutually_exclusive_group(required=True)
    group.add_argument("--interval", type=float, help="Seconds between waiting reviews")
    group.add_argument("--disable", action="store_true")
    item = command("resolve", help="After checking delivery, resolve the current uncertain/rejected batch")
    item.add_argument("id")
    item.add_argument("--outcome", choices=("accepted", "not-delivered"), required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    directory = state_directory(args.state_dir)
    try:
        if args.command == "daemon":
            if args.action == "run":
                if not 1 <= args.max_checks <= 64 or not 1 <= args.max_sends <= 64 or not 0 <= args.retention_seconds <= 86400:
                    raise ValueError("worker limits must be 1..64 and retention 0..86400 seconds")
                from .daemon import run_daemon
                run_daemon(directory, args.max_checks, args.max_sends, args.retention_seconds)
                return 0
            result = daemon_start(directory) if args.action == "start" else request(directory, "ping" if args.action == "status" else "stop")
        elif args.command == "submit":
            spec = json.loads(args.file.read_text())
            # cwd is relative to the specification, not the submitting terminal.
            spec["cwd"] = str((args.file.resolve().parent / spec.get("cwd", ".")).resolve())
            result = request(directory, "submit", spec=spec)
        elif args.command == "list":
            owner = args.owner
            if args.mine:
                owner = os.environ.get("CODEX_THREAD_ID")
                if not owner:
                    raise ValueError("--mine needs CODEX_THREAD_ID; alternatively use --owner THREAD_UUID")
            result = request(directory, "list", owner=owner, include_finished=args.include_finished)
        elif args.command == "show":
            result = request(directory, "show", id=args.id, buffer=args.buffer)
        elif args.command == "emit":
            value = json.loads(args.file.read_text())
            result = request(directory, "emit", id=args.id, events=value if isinstance(value, list) else [value])
        elif args.command == "review":
            result = request(directory, "review", id=args.id, interval_seconds=None if args.disable else args.interval)
        elif args.command == "resolve":
            result = request(directory, "resolve", id=args.id, outcome=args.outcome)
        else:
            result = request(directory, args.command, id=args.id)
        if args.command == "list" and not args.json:
            print(table(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
