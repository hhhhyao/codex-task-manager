#!/usr/bin/env python3
"""Example notice for delegated external work; read quick local results inline."""

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--immediate", action="store_true",
                        help="Bypass the window only when this outcome needs early attention")
    args = parser.parse_args()
    try:
        value = json.loads(args.manifest.read_text())
        if value.get("run_id") != args.run_id:
            raise ValueError("manifest belongs to a different run")
        state = value["state"]
        if state in {"pending", "running"}:
            print(json.dumps({"events": [], "status": state}))
        elif state in {"completed", "failed"}:
            print(json.dumps({"events": [{"id": f"{args.run_id}:{state}",
                "text": f"Run {args.run_id} {state}; inspect the authoritative result.",
                "important": state == "failed", "immediate": args.immediate,
                "detail_path": str(args.manifest.resolve())}], "status": state, "done": True}))
        else:
            raise ValueError("unknown manifest state")
        return 0
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
