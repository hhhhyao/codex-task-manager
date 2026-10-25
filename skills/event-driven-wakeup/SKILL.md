---
name: event-driven-wakeup
description: Arrange background follow-up when an agent temporarily turns attention away from medium- or long-running external work, uncertain readiness, or an explicit schedule/monitor. Use Codex Task Manager for observation, bounded notification buffers and waiting reviews; Repowire delivers to the intended session. Keep quick work and results naturally consumed by the current execution flow inline.
---

# Event-driven Wakeup

Delegate attention to external work while continuing other work or becoming idle.
The agent chooses what deserves follow-up and when intervention is useful; Task
Manager manages observation and temporary buffers, and Repowire delivers notices.
The original executor runs the workload; the agent makes the next decision.

## Choose whether to delegate

Start with the current execution flow. Read quick command/query results directly,
batch predetermined short steps in an ordinary work script, and reuse an existing
result handle or reliable completion path when normal work will actually consume
its result. No Task Manager lookup or registration
is needed just because a command finished or another small step can begin.

Delegate when independent follow-up prevents repeated checking or a missed result:
work continues while the agent turns elsewhere, readiness may take an uncertain
wait, or a schedule/ongoing monitor was explicitly arranged. This can be within
the same turn or across idle time. Judge the workload's remaining wait and natural
consumption path, not the duration of a fast status check; no fixed time cutoff.

In the existing plan, identify the awaited condition, why independent follow-up
helps, what should cause a notice, a one-sentence task `description`, and a concrete `next_action`. "Continue the
Goal" alone is not a useful continuation. Already-ready results should be handled
now. See [observation patterns](references/events.md) for examples.

## Arrange the observation

After choosing delegation, use `ctm daemon status` and `ctm list --mine` (or
`--owner THREAD_UUID`) to reuse equivalent observers. All agents share one state
directory/daemon. One task groups outcomes to be handled together; script count
does not define queue count. Independently managed work can use separate tasks.
Do not create a daemon or tmux window per task.

| Need | Read when relevant |
| --- | --- |
| Submit, inspect, cancel or recover a task | [Runtime](references/runtime.md) |
| Select signals and organize a work stage | [Observation patterns](references/events.md) |
| Accumulate and send notices; manage waiting reviews | [Buffers and reviews](references/buffers.md) |
| Pause/resume a formal Goal during idle waiting | [Goal continuation](references/goals.md) |

Save exact owner/recipient peer and thread UUID, workload/run identity, plan path,
notification condition, `next_action` and cancellation handle. Launch real work
once through its executor, validate an authoritative bounded check, then submit
and retain its ID. Prefer callbacks/manifests or bulk checks over model polling.

Use `once` for one business trigger, `repeat` for ongoing observation. A once task
may check repeatedly and receive reviews. Return `done: true` when observation
ends; checks never re-submit work. Observation errors are not workload failure.

## Observe without notifying every change

Use `status` for a short queryable view and source files for results/history.
`events` requests attention: return an empty array when normal execution or the
next natural checkpoint will consume the result. Registration, a new fact, or a
failed short check alone does not justify an extra message. No model is needed
for each poll or buffer formatting.

Notify when enough results enable the planned work, a problem needs intervention,
or a delegated wait needs review. Keep deterministic short sequences in the
original work script and observe their stage outcome; preserve intermediate
results at the source. Failures may matter without requiring immediate delivery.
`important` marks text; `immediate`/`flush` need a reason to send before the chosen
buffer window. Buffering organizes justified notices; it cannot fix overuse.

Use stable fact IDs, preserve distinct member outcomes and replace only explicit
progress snapshots. Keep sampling out of events. Source-side one-time/episode
state prevents repeats after the bounded recent-ID cache expires.

## Receive, review and close

Read the direct notice, reconcile the latest task plan, and open needed artifacts.
There is no inbox collection or message settlement. Record actual handling in
the original plan; transport acceptance does not establish that work was handled.

For a substantial delegated wait, choose `review_interval_seconds` or verify an
equivalent path that prompts task-specific review if waiting persists; a routine
status update alone is insufficient. Reviews apply while attention is elsewhere, whether the agent
is working or idle. Inspect progress/prerequisites; overdue does not mean failed.
Once normal follow-up of this work resumes, disable or adjust that wait's review
with `ctm review ID`; re-enable it when delegating attention again. Unrelated work
or a heartbeat does not by itself end the wait. The daemon cannot infer these
attention changes from a successful send. See [review lifecycle](references/buffers.md#waiting-reviews).

Cancel obsolete observations with `ctm cancel ID`; retain ongoing monitors only
while their policy is needed. Cancellation does not stop the external workload
or recall sent messages. Check late notices against current task identity.
Continue useful work; save a handoff and end the turn only when nothing else can
proceed. Timestamp user-facing progress/handoffs/completion with current UTC;
ordinary notices need no reflexive acknowledgement.

## Formal Goal waits

An observer running does not require pausing an active Goal. When an event needs
a long wait and no useful independent work remains, confirm observation/review,
save the wake handoff, then pause under the user's instruction for this workflow.
Verify paused, report and end the turn. On wake, resume the same Goal only when
current conditions permit useful work (including diagnosis); preserve objective,
identity, budget and accounting. Standing authorization covers its stated scope
without repeated questions; later manual pauses/cancellation take precedence.
A notice is not a Goal state transition. Follow [Goal control](references/goals.md).
