# Choose useful background follow-up

| Situation | Approach |
| --- | --- |
| A query/command completes in seconds and its result is next in the current flow | Run/read it inline; no observer or extra notice. |
| Several predetermined short validations | Batch them in the original work script; read the stage result. |
| Work has a reliable result handle or completion path the agent will naturally consume | Reuse it; do not duplicate that path with another observer. |
| External evaluation/download continues while the agent handles other work | Observe the authoritative outcome; notify when enough results enable the named action. |
| Resource/readiness condition has uncertain duration | Check actual usable readiness; arrange review while attention is elsewhere. |
| Long-lived health/progress monitor | Update queryable state; notify for a new intervention, relevant recovery or agreed milestone. |
| Explicit deadline or periodic assignment | Use the requested schedule and per-occurrence identity. |

Time alone is not admission: a 1-second query of a 3-hour job can be a good
observer. A quick job with a directly available result usually needs none. Being
in an active turn does not remove the need for delegated follow-up; becoming idle
is not required. Also check whether the existing executor already supplies enough
notification/context to avoid creating a second completion path.

## Choose the work unit

Group what the agent should handle together. For example, a long evaluation can
notify once two variants are comparable; it need not publish every completed
member. Known sample → validation → report steps can be an ordinary work script,
launched once through its executor. The observer only reads its manifest. If an
intermediate outcome needs a new agent decision, notify with that decision and
its evidence. Do not hide a real decision or unrelated failure merely to wait
for all results.

Renaming a watcher or retrying a check creates a new technical identity, not
necessarily a new need for agent attention. Choose notice conditions around the
business phase; preserve distinct run outcomes in source results.

## Write the observer

Prefer callbacks, supervisor receipts or atomic manifests. Validate run identity,
schema, freshness and expected members. Missing/unparseable state is not confirmed
waiting; a vanished PID is not proof of success. Bound checks and network queries;
never launch the workload again inside a recurring check.

```python
observation = inspect_existing_work()
save_source_results(observation)
notice = needs_agent_attention(observation, current_plan, source_state)
print(json.dumps({"status": observation.brief,
                  "events": make_notices(observation) if notice else [],
                  "done": observation.round_ended}))
```

The task-specific predicate asks why a message is useful beyond the natural
consumption path. It need not call a model. Use `status`/source artifacts for
routine progress. Source one-time/member/episode state prevents repeats; it does
not stand for work the receiving agent actually performed.

For finite once rounds, watch the remaining relevant members. A permanently true
"any job terminal" predicate cannot generate a fresh notice each poll. Include
failures in the useful outcome; choose early delivery only when delay matters,
not simply because a result is terminal. Keep text readable and evidence paths
specific; follow the latest plan instead of appending stale instructions.
