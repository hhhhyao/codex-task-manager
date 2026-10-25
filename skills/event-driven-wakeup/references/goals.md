# Formal Goal continuation

A wake starts/steers a turn; it does not change Goal status. Observation may run
while the agent does other work in the same turn. End an ordinary turn only when
no useful work remains. Formal Goals need separate state control when external
waiting should stop their automatic continuation.

Use this cycle when an event needs a long external wait and no useful independent
work can continue: arrange the wake and handoff, pause, then resume the same Goal
after a wake only when current conditions permit useful continuation. Short waits
need not force this full cycle. An active observer alone does not require a pause.

## Register, hand off and pause

Keep the original Goal's identity/objective, creation time, budget and usage in
the existing task handoff, together with:

- The user's pause/resume instruction and which waits it covers.
- Exact original thread, workload/run IDs, `ctm` task ID and result/log paths.
- `next_action`: the concrete action after wake, its prerequisites, and why waiting remains.
- The [long-wait review](buffers.md) and what to inspect if the expected event does not arrive.

These are continuation details in the task record, not additional daemon schema.
Arrange and confirm the observation before handoff; an already-actionable result
should be handled now. Finish independent authorized work first. When only
long external waiting remains, pause under the user's instruction covering the
Goal, using `update_goal(status="paused")` if supported. Verify paused, report
the wait briefly and end the turn immediately. Do not follow a successful pause
with another work loop.

A user request to use this long-wait pause/wake/resume workflow is standing
authorization within its stated scope; reuse it without asking each time.
Installing/invoking the skill alone is not that
authorization. If none applies, obtain the user's intent once. Later manual
pause, cancellation or objective changes override earlier instructions.

Ending a response may not stop an active Goal. Do not manufacture blocked/complete,
replace the objective, reset accounting or bypass a budget/usage limit to stop turns.

## Resume for the next useful action

Match the event to the latest task plan and actual Goal. Resume when one useful
authorized step becomes possible, not only when the whole project or every
parallel run is ready. A failed job can enable diagnosis; one completed evaluation
can enable comparison. A blocker for a later deployment need not block either.
An overdue review may enable diagnosis even while the original result is absent.
Do not resume solely because a message arrived; check the current prerequisites,
latest instructions and the recorded next action.

| State on receipt | Action |
| --- | --- |
| Active | Continue useful work. |
| Paused | Resume only under applicable explicit authorization. |
| Blocked | Reassess prerequisites for the chosen next action; use supported authorized continuation when ready. |
| Missing, complete, budget/usage limited | Preserve state; do not recreate/reset it. |

Use supported controls in the owning runtime, verify the same Goal as active,
then execute the useful action. A message asking for resume is not the transition;
some model tools cannot set active, so inspect schemas.
When normal follow-up of this work resumes, adjust/disable its waiting review;
other still-delegated conditions can keep their reviews. Re-enable an appropriate
interval if attention turns away again. Sending a notice is not proof of takeover.

If the blocker is unchanged and nothing useful can proceed, reuse/arrange the
remaining observation and next review. Do not resume merely to rediscover the same
blocker. After useful work, another wait uses the same standing authorization:
**work → register/review → authorized pause → event → authorized resume → work**.
An observation error requires diagnosis; it is not proof of readiness or failure.

Do not force ownership by editing state, removing locks or killing clients.
Any client-input adapter must verify the intended thread and preserve drafts.

## Native control and evidence

Codex 0.164.0 exposes experimental `thread/goal/get` and `thread/goal/set`.
Initialize with `experimentalApi=true` on the existing shared App Server.
Its default control socket
`${CODEX_HOME:-~/.codex}/app-server-control/app-server-control.sock` uses
WebSocket. For an explicitly authorized resume:

```python
rpc("thread/goal/get", {"threadId": original_thread_id})
rpc("thread/goal/set", {"threadId": original_thread_id,
                        "status": "active", "origin": "user"})
rpc("thread/goal/get", {"threadId": original_thread_id})
```

`origin="user"` records real authorization, not permission granted by a flag.
Read before mutation; verify resumable state and unchanged thread, objective,
creation time and budget, with nondecreasing usage. Omit objective/budget fields.
Re-read uncertain outcomes before retrying; do not start a second server to mutate
a live owned thread.

Validated with Repowire v0.21.0 / Codex 0.164.0: a raw native message reached the
original paused session and authorized native control restored the same Goal,
preserving identity and usage. Receipt, active read-back and later continuation
remain separate evidence. If resume control is unavailable, report that precise
limitation; event delivery may still work. See [official Goal documentation](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex).
