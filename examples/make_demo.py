#!/usr/bin/env python3
"""Prepare a scaled external-wait demo. Real quick results should be read inline."""

import argparse
import json
from pathlib import Path
import sys
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--peer-id", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    uuid.UUID(args.thread_id)
    args.directory.mkdir(parents=True, exist_ok=False)
    root = args.directory.resolve()
    (root / "status.json").write_text(json.dumps({"run_id": "demo", "state": "running"}) + "\n")
    (root / "plan.txt").write_text("Inspect the demo result when complete, or investigate if the waiting review fires.\n")
    spec = {"name": "manifest-demo", "owner": {"peer_id": args.peer_id, "thread_id": args.thread_id},
            "cwd": str(root), "mode": "once", "interval_seconds": 2, "window_seconds": 10,
            "review_interval_seconds": 300,
            "check": [sys.executable, str(Path(__file__).with_name("check_manifest.py")),
                      "--manifest", str(root / "status.json"), "--run-id", "demo"],
            "next_action": "Inspect the completed demo result, or diagnose the wait if the review fires.",
            "plan_path": str(root / "plan.txt")}
    (root / "task.json").write_text(json.dumps(spec, indent=2) + "\n")
    print(root / "task.json")


if __name__ == "__main__":
    main()
