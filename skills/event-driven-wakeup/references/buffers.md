# Temporary notification buffers

Related producers share one task ID; independent tasks have independent buffers.
The agent receives readable facts directly. There is no session-wide acknowledgement
gate or historical inbox to read. Original results stay with the workload.

## Admit notices before batching

`status` updates the current queryable view; `events` requests a message. Decide
whether external attention is needed before publishing. A useful new fact can
remain in the source until the agent's normal checkpoint. A bigger window does
not solve registering every short command or emitting every progress update.

Choose `window_seconds` for notices that may arrive together. The first event
anchors it; later events do not extend it. `max_events` also triggers sending.
`important` marks text; `immediate` or `ctm flush ID` sends earlier and needs a
reason why waiting for the window would hurt the next action. Failures are not
automatically immediate. Sent notices cannot absorb events that arrive later;
sequential work may genuinely need separate decisions.

```json
{
  "id": "run-42:member-a:failed",
  "text": "Member A failed; include its trace in the planned comparison.",
  "important": true,
  "detail_path": "/absolute/run-42/member-a/result.json"
}
```

Only stable `id` and brief nonempty `text` are required. Optional `at` is the
actual timezone-aware event time; otherwise reception time is used. Timestamps
do not create new fact identities. `replace_key` replaces only unsent progress
snapshots, never independent member outcomes. The finite recent-ID cache is not
permanent deduplication; retain source one-time/episode state for long monitors.

Formatting marks important facts, orders by timestamp and adds task identity,
`next_action` and source/plan paths. Keep next action specific to this stage,
not a repeated whole-project instruction. No model is used for buffer formatting.
The transport adds batch identity and actual UTC send time.

Default body limit is 6000 characters; larger batches split without dropping
facts. Oversized individual events are rejected so producers can shorten them
and reference source results. Buffer capacity defaults to 128 KiB; handle
rejection and preserve the source. Sending freezes one batch while arrivals go
into the next buffer. Accepted/queued sends release the body; no event archive
or agent settlement is kept. Explicit rejection retries are bounded (default 3).
Uncertain sends retain their unresolved batch, not blocking other tasks; use
[recovery](runtime.md) after checking actual delivery.

## Waiting reviews

`review_interval_seconds` belongs to a delegated wait, not automatically to the
entire lifetime of a repeat observer. Use it when this work still needs independent
follow-up while the agent is busy elsewhere or idle. The interval, check cadence
and buffer window serve different purposes and need not match.

At each configured interval the daemon creates one immediate review if the
observation is open, after a due check can report current results. Reviews do not
end once observations. Read current progress and prerequisites; being overdue is
not failure. Review is an attention request too, so choose a useful, sparse cadence.

When the agent resumes normal follow-up of this work, adjust or disable its review:

```bash
ctm review TASK_ID --disable
# When attention is delegated again, choose the appropriate interval in seconds:
ctm review TASK_ID --interval 1800
```

This is a change of follow-up policy, not per-message acknowledgement. Keep
reviews for conditions still delegated; doing unrelated work does not resolve
those waits. Ongoing monitors can keep observing with review disabled, and can
still publish justified business notices. Explicitly requested periodic work is
its own schedule, not a review that should be disabled on every receipt.

The daemon cannot know which work the agent is now following. Enabling review
means it repeats until disabled, done or cancelled, including during active turns.
Heartbeat, an empty buffer or transport acceptance does not reset it. Unsent
reviews replace one another and restart coalesces missed intervals. An enabled
review can flush other buffered facts early; account for that when choosing its
scope. Completion drops obsolete unsent reviews. Inspect next/last review and
count with `ctm show` when recovering the wait.
