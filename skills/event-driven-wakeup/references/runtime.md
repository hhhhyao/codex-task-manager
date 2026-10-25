# Shared task runtime

Use this runtime after choosing independent follow-up; quick inline work needs
no daemon lookup or registration. `ctm` / `codex-task-manager` is installed
with `uv tool install --editable /path/to/codex-task-manager` or a virtualenv.
Repowire is set up separately with `repowire setup`; see its official
[Codex integration](https://docs.repowire.io/use/features/connect-codex/).
Verify an addressable native `codex-app-server` peer for the intended original
thread. MCP tools alone do not establish incoming wake delivery.

```bash
ctm daemon status
ctm daemon start
ctm list --mine --json
ctm submit /absolute/task.json
ctm show TASK_ID
ctm cancel TASK_ID
```

Start only when no matching daemon is available. Default shared state is
`${XDG_STATE_HOME:-~/.local/state}/codex-task-manager`, overridable with
`CODEX_TASK_MANAGER_HOME` or global `--state-dir`. All producers use that instance.
`list --all` shows active observations across agents; `--owner THREAD_UUID` works
when `CODEX_THREAD_ID` is not available. `show` gives current status, next check/
flush/review, last trigger/send, recent error and buffer counts. It does not
return event history. `show --buffer` explicitly inspects current buffered events.

Submit JSON, for example:

```json
{
  "name": "compare-current-runs",
  "owner": {"peer_id": "EXACT_PEER_ID", "thread_id": "ORIGINAL_THREAD_UUID"},
  "cwd": "/absolute/task",
  "check": ["/absolute/python", "/absolute/task/check.py"],
  "mode": "once",
  "interval_seconds": 60,
  "window_seconds": 120,
  "review_interval_seconds": 1800,
  "next_action": "Compare A/B metrics when both are ready; inspect their run logs if the wait needs review.",
  "plan_path": "/absolute/task/plan.txt"
}
```

Replace identity placeholders with verified values and choose a useful review
interval. Default `target` is the owner. Resolve the saved thread before changing
a failed peer route; do not pick the latest thread or display name.
Native busy delivery may steer an active turn;
this runtime does not guarantee idle-only delivery or cold-resume offline threads.

`once` stops checks after the first business event and drains already accepted contents;
review/error notices do not end it. `review_interval_seconds` repeats waiting
checks until observation ends; status shows next/last review and the count.
Use `ctm review ID --interval SECONDS` to adjust, or `--disable` to stop reviews.
Keep review tied to the current delegated wait, including when doing other work;
adjust/disable it when normal follow-up of that work resumes. The runtime does
not detect agent attention or infer handling from send acceptance.
`repeat` runs until `done` or cancellation. Omit `check` and timing fields for
callbacks, then `ctm emit ID /absolute/event.json`. `fire_at` is an absolute timer;
`interval_seconds` without a check is a repeating timer (or one delayed tick for
once). Calendar cron remains available through Repowire/external schedulers.

Checks return JSON `{"status":"running", "events":[], "done":false}` on exit 0.
`status` is queryable progress; nonempty `events` requests external attention.
Write results to the source and leave events empty when the current flow or its
next natural checkpoint will read them. Empty events do not stop observation.
Exit 1 with no output means pending; other exits, timeouts or malformed results
are observation errors. Default check timeout is 15s, stdout/stderr each limited
to 64 KiB. Repeated errors notify once per error episode (default after 3).
Do not start the workload inside the recurring check.

Normal daemon restart preserves buffers. Interrupted checks pause for inspection;
use `ctm resume ID` after verifying the old recorded PID. Unknown sends are not
automatically repeated. Inspect the actual session/Repowire trace before
`ctm resolve ID --outcome accepted` or `--outcome not-delivered`.
Queued/runtime acceptance and actual model handling remain separate evidence.
Never clear uncertainty simply to resend. Cancel cannot recall in-flight messages.

Cancellation targets the exact observation; external workload cancellation remains
a separate action. Ended task summaries expire after 5 minutes by default.
