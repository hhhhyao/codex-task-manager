"""Synchronous state transitions; no network or subprocess work holds a transaction."""

import copy
import json
import time
import uuid

from .common import digest, number, stamp, timestamp, validate_spec


TERMINAL = {"finished", "cancelled"}


def render(spec, events, reason):
    ordered = sorted(events, key=lambda e: (timestamp(e["at"]), e["received_order"]))
    lines = [f"Task: {spec['name']}", f"Task ID: {spec['id']}", f"Trigger: {reason}"]
    for event in ordered:
        mark = " [IMPORTANT]" if event.get("important") else ""
        text = event["text"].replace("\n", "\n  ")
        lines.append(f"{event['at']}{mark} {text}")
        if event.get("detail_path"):
            lines.append(f"  Details: {event['detail_path']}")
    if spec.get("next_action"):
        lines.append(f"Next action: {spec['next_action']}")
    if spec.get("plan_path"):
        lines.append(f"Plan: {spec['plan_path']}")
    return "\n".join(lines)


class Manager:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def submit(self, raw):
        spec = validate_spec(raw)
        try:
            old, _ = self.store.get(spec["id"])
        except ValueError:
            pass
        else:
            if old != spec:
                raise ValueError("task ID already exists with a different specification")
            return self.show(spec["id"])
        now = self.clock()
        next_check = now if "check" in spec else timestamp(spec["fire_at"]) if "fire_at" in spec else now + spec["interval_seconds"] if "interval_seconds" in spec else None
        state = {"created_at": now, "terminal": None, "terminal_at": None, "paused": False,
                 "source_done": False, "next_check_at": next_check, "checking": False,
                 "check_pid": None, "buffer": [], "seen": [], "sequence": 0, "outbox": None,
                 "next_flush_at": None, "last_check_at": None, "last_successful_check_at": None,
                 "last_trigger_at": None, "last_sent_at": None, "consecutive_errors": 0,
                 "last_error": None, "workload_status": "unknown", "error_episode": 0,
                 "error_notified": False, "timer_sequence": 0, "review_revision": 1,
                 "review_interval_seconds": spec.get("review_interval_seconds"),
                 "next_review_at": now + spec["review_interval_seconds"] if "review_interval_seconds" in spec else None,
                 "last_review_at": None, "review_count": 0}
        self.store.insert(spec, state)
        return self.show(spec["id"])

    def _ingest(self, spec, state, events, close_once=True):
        if not isinstance(events, list) or len(events) > 1000:
            raise ValueError("events must be an array of at most 1000 brief events")
        now, added = self.clock(), 0
        for raw in events:
            if not isinstance(raw, dict) or set(raw) - {"id", "at", "text", "important", "immediate", "replace_key", "detail_path", "kind"}:
                raise ValueError("invalid event fields")
            if not isinstance(raw.get("id"), str) or not 1 <= len(raw["id"]) <= 200:
                raise ValueError("event requires a stable id of 1..200 characters")
            if not isinstance(raw.get("text"), str) or not raw["text"].strip() or len(raw["text"]) > 4000:
                raise ValueError("event text must contain 1..4000 characters")
            for field in ("important", "immediate"):
                if field in raw and not isinstance(raw[field], bool):
                    raise ValueError(f"{field} must be boolean")
            for field in ("replace_key", "kind"):
                if field in raw and (not isinstance(raw[field], str) or not 1 <= len(raw[field]) <= 200):
                    raise ValueError(f"{field} must be a short nonempty string")
            if "detail_path" in raw:
                from pathlib import Path
                if not isinstance(raw["detail_path"], str) or len(raw["detail_path"]) > 2000 or not Path(raw["detail_path"]).is_absolute():
                    raise ValueError("detail_path must be an absolute path")
            at = stamp(timestamp(raw["at"])) if "at" in raw else stamp(now)
            # Observation timestamps do not turn repeated facts into new events.
            fingerprint = digest({k: v for k, v in raw.items() if k != "at"})
            known = next((e for e in state["buffer"] + (state["outbox"]["events"] if state["outbox"] else []) + state["seen"] if e["id"] == raw["id"]), None)
            if known:
                if known["digest"] != fingerprint:
                    raise ValueError("event ID reused for changed facts")
                continue
            state["sequence"] += 1
            event = {**raw, "at": at, "digest": fingerprint, "received_order": state["sequence"]}
            if len(render(spec, [event], "buffer window or condition reached")) > spec["max_message_chars"]:
                raise ValueError("event exceeds message budget; shorten text and reference its source")
            if event.get("replace_key"):
                # Only explicit progress snapshots replace one another, within this buffer.
                newer = any(e.get("replace_key") == event["replace_key"] and timestamp(e["at"]) > timestamp(event["at"]) for e in state["buffer"])
                if newer:
                    state["seen"].append({"id": raw["id"], "digest": fingerprint})
                    state["seen"] = state["seen"][-spec["seen_limit"]:]
                    continue
                state["buffer"] = [e for e in state["buffer"] if e.get("replace_key") != event["replace_key"]]
            state["buffer"].append(event)
            if len(state["buffer"]) > 1000 or len(json.dumps(state["buffer"]).encode()) > spec["max_buffer_bytes"]:
                raise ValueError("buffer capacity reached; preserve the source and retry after sending/recovery")
            state["seen"].append({"id": raw["id"], "digest": fingerprint})
            state["seen"] = state["seen"][-spec["seen_limit"]:]
            if state["next_flush_at"] is None:
                state["next_flush_at"] = now + spec["window_seconds"]
            if event.get("immediate") or len(state["buffer"]) >= spec["max_events"]:
                state["next_flush_at"] = min(state["next_flush_at"], now)
            state["last_trigger_at"] = now
            added += 1
        if added and close_once and spec["mode"] == "once":
            state["source_done"], state["next_check_at"], state["next_review_at"] = True, None, None
            self._drop_unsent_reviews(state)
        return added

    @staticmethod
    def _drop_unsent_reviews(state):
        state["buffer"] = [event for event in state["buffer"] if event.get("kind") != "wait_review"]
        if not state["buffer"]:
            state["next_flush_at"] = None

    def emit(self, task_id, events):
        with self.store.edit(task_id) as (spec, state):
            if state["terminal"]:
                raise ValueError("task has ended; late producers must stop")
            added = self._ingest(spec, state, events)
        return {"added": added, "task": self.show(task_id)}

    def show(self, task_id, include_buffer=False):
        spec, state = self.store.get(task_id)
        outbox = state["outbox"]
        status = (state["terminal"] or ("paused" if state["paused"] else
                  "delivery_" + outbox["status"] if outbox else "checking" if state["checking"] else
                  "check_error" if state["consecutive_errors"] else "buffering" if state["buffer"] else "waiting"))
        time_fields = ("created_at", "terminal_at", "next_check_at", "next_flush_at", "next_review_at", "last_review_at",
                       "last_check_at", "last_successful_check_at", "last_trigger_at", "last_sent_at")
        result = {"id": task_id, "name": spec["name"], "owner": spec["owner"], "target": spec["target"],
                  "mode": spec["mode"], "status": status, "source_done": state["source_done"],
                  "next_action": spec.get("next_action"),
                  "interval_seconds": spec.get("interval_seconds"), "buffered": len(state["buffer"]),
                  "sending_events": len(outbox["events"]) if outbox else 0,
                  "delivery": {k: v for k, v in outbox.items() if k not in {"events", "text"}} if outbox else None,
                  "workload_status": state["workload_status"], "last_error": state["last_error"],
                  "consecutive_errors": state["consecutive_errors"], "check_pid": state["check_pid"],
                  "check": spec.get("check"), "cwd": spec["cwd"],
                  "review_interval_seconds": state["review_interval_seconds"], "review_count": state["review_count"],
                  "review_revision": state["review_revision"]}
        result.update({key: stamp(state[key]) if state[key] is not None else None for key in time_fields})
        if include_buffer:
            result["buffer"] = state["buffer"]
        return result

    def list(self, owner=None, include_finished=False):
        return [self.show(spec["id"]) for spec, state in self.store.all()
                if (include_finished or not state["terminal"]) and
                (owner is None or spec["owner"]["thread_id"] == owner)]

    def flush(self, task_id):
        with self.store.edit(task_id) as (_, state):
            if state["buffer"]:
                state["next_flush_at"] = self.clock()
        return self.show(task_id)

    def cancel(self, task_id):
        with self.store.edit(task_id) as (_, state):
            state.update(terminal="cancelled", terminal_at=self.clock(), buffer=[], outbox=None,
                         seen=[], source_done=True, next_check_at=None, next_flush_at=None, next_review_at=None,
                         checking=False, check_pid=None)
        return self.show(task_id)

    def configure_review(self, task_id, interval):
        if interval is not None:
            number(interval, "review_interval_seconds", 0.05)
        with self.store.edit(task_id) as (_, state):
            if state["terminal"] or state["source_done"]:
                raise ValueError("ended observation requires a new task")
            state["review_revision"] += 1
            state.update(review_interval_seconds=interval,
                         next_review_at=self.clock() + interval if interval is not None else None)
            self._drop_unsent_reviews(state)
        return self.show(task_id)

    def recover(self):
        for spec, _ in self.store.all():
            with self.store.edit(spec["id"]) as (_, state):
                # Upgrade early 0.1 task records; live timers retain their next deadline.
                if "next_review_at" not in state:
                    old_deadline = state.get("review_at")
                    interval = spec.get("review_interval_seconds")
                    if interval is None and old_deadline is not None:
                        interval = max(0.05, old_deadline - state["created_at"])
                    next_review = None
                    if not state["terminal"] and not state["source_done"] and interval is not None:
                        next_review = self.clock() + interval if state.get("review_fired") else old_deadline or self.clock() + interval
                    state.update(review_interval_seconds=interval,
                                 next_review_at=next_review,
                                 last_review_at=None, review_count=1 if state.get("review_fired") else 0)
                    state.pop("review_at", None)
                    state.pop("review_fired", None)
                if state["checking"]:
                    state.update(checking=False, paused=True, last_error="Check interrupted by daemon exit; inspect previous check_pid before resume")
                if state["outbox"] and state["outbox"]["status"] == "sending":
                    state["outbox"].update(status="ambiguous", error="Daemon exited during delivery; inspect receipt/session before resolving")

    def resume(self, task_id):
        import os
        with self.store.edit(task_id) as (_, state):
            if state["terminal"]:
                raise ValueError("task has ended")
            if state["check_pid"]:
                try:
                    os.kill(state["check_pid"], 0)
                except ProcessLookupError:
                    pass
                else:
                    raise ValueError("previous check PID still exists; inspect it before resuming")
            state.update(paused=False, check_pid=None, last_error=None)
            if not state["source_done"]:
                state["next_check_at"] = self.clock()
        return self.show(task_id)

    def begin_check(self, task_id):
        with self.store.edit(task_id) as (_, state):
            state.update(checking=True, next_check_at=None)

    def check_started(self, task_id, pid):
        with self.store.edit(task_id) as (_, state):
            if not state["terminal"]:
                state["check_pid"] = pid

    def check_result(self, task_id, result=None, error=None):
        spec, state = self.store.get(task_id)
        if state["terminal"]:
            return
        # Validate in one transaction; an invalid multi-event result publishes nothing.
        try:
            if error:
                raise ValueError(error)
            if not isinstance(result, dict) or set(result) - {"events", "done", "status"}:
                raise ValueError("check must return an object with events, optional done/status")
            if "done" in result and not isinstance(result["done"], bool):
                raise ValueError("done must be boolean")
            if not isinstance(result.get("status", "unknown"), str) or len(result.get("status", "")) > 500:
                raise ValueError("status must be a string of at most 500 characters")
            with self.store.edit(task_id) as (spec, state):
                self._ingest(spec, state, result.get("events", []))
                state.update(checking=False, check_pid=None, last_check_at=self.clock(),
                             last_successful_check_at=self.clock(), consecutive_errors=0,
                             last_error=None, error_notified=False,
                             workload_status=result.get("status", "unknown"))
                if result.get("done"):
                    state.update(source_done=True, next_review_at=None)
                    self._drop_unsent_reviews(state)
                state["next_check_at"] = None if state["source_done"] else self.clock() + spec["interval_seconds"]
                self._finish_if_drained(state)
        except (ValueError, TypeError, KeyError) as exc:
            with self.store.edit(task_id) as (spec, state):
                if not state["consecutive_errors"]:
                    state["error_episode"] += 1
                state["consecutive_errors"] += 1
                state.update(checking=False, check_pid=None, last_check_at=self.clock(),
                             last_error=str(exc)[:1000], next_check_at=self.clock() + spec["interval_seconds"])
                if state["consecutive_errors"] >= spec["error_notify_after"] and not state["error_notified"]:
                    event = {"id": f"observer-error:{state['error_episode']}", "kind": "observer_error",
                             "text": "Observation repeatedly failed; inspect the check. " + str(exc)[:240],
                             "important": True, "immediate": True}
                    try:
                        trial = copy.deepcopy(state)
                        self._ingest(spec, trial, [event], close_once=False)
                        state.update(trial, error_notified=True)
                    except ValueError:
                        pass  # Keep the visible error and retry publishing after capacity recovers.

    def _finish_if_drained(self, state):
        if state["source_done"] and not state["buffer"] and not state["outbox"]:
            state.update(terminal="finished", terminal_at=self.clock(), seen=[], next_review_at=None, next_check_at=None)

    def tick(self):
        now = self.clock()
        for spec, old in self.store.all():
            if old["terminal"]:
                continue
            with self.store.edit(spec["id"]) as (_, state):
                needs_fresh_check = state["next_review_at"] is not None and (state["last_check_at"] is None or state["last_check_at"] < state["next_review_at"])
                check_due = needs_fresh_check and "check" in spec and (state["checking"] or (state["next_check_at"] is not None and state["next_check_at"] <= now and not state["paused"]))
                check_due = check_due or ("check" not in spec and state["next_check_at"] is not None and state["next_check_at"] <= now and not state["paused"])
                if state["next_review_at"] is not None and now >= state["next_review_at"] and not state["source_done"] and not check_due:
                    count = state["review_count"] + 1
                    event = {"id": f"wait-review:{state['review_revision']}:{count}", "kind": "wait_review",
                             "text": f"Waiting review {count} reached {stamp(state['next_review_at'])}. Inspect current progress and prerequisites; this is not proof of failure.",
                             "replace_key": "ctm-wait-review", "important": True, "immediate": True}
                    try:
                        trial = copy.deepcopy(state)
                        self._ingest(spec, trial, [event], close_once=False)
                        state.update(trial, review_count=count, last_review_at=now,
                                     next_review_at=now + state["review_interval_seconds"])
                    except ValueError as exc:
                        state["last_error"] = str(exc)
                if "check" not in spec and state["next_check_at"] is not None and now >= state["next_check_at"] and not state["paused"] and not state["source_done"]:
                    due = state["next_check_at"]
                    try:
                        trial = copy.deepcopy(state)
                        trial["timer_sequence"] += 1
                        self._ingest(spec, trial, [{"id": f"timer:{trial['timer_sequence']}", "at": stamp(due),
                                                  "text": spec.get("message", "Scheduled continuation is due."), "immediate": True}])
                        state.update(trial)
                    except ValueError as exc:
                        state["last_error"] = str(exc)
                    state["next_check_at"] = None if state["source_done"] else now + spec["interval_seconds"]
                self._finish_if_drained(state)

    def freeze(self, task_id):
        with self.store.edit(task_id) as (spec, state):
            if state["terminal"] or state["outbox"] or not state["buffer"] or state["next_flush_at"] > self.clock():
                return None
            reason = "immediate event" if any(e.get("immediate") for e in state["buffer"]) else "buffer window or condition reached"
            ranked = sorted(state["buffer"], key=lambda e: (not e.get("immediate", False), not e.get("important", False), e["received_order"]))
            selected = []
            for event in ranked:
                if len(render(spec, selected + [event], reason)) <= spec["max_message_chars"]:
                    selected.append(event)
            if not selected:
                raise ValueError("message budget too small for buffered event")
            batch = {"id": str(uuid.uuid4()), "status": "ready", "events": selected,
                     "text": render(spec, selected, reason), "attempts": 0,
                     "next_attempt_at": self.clock(), "error": None}
            selected_ids = {e["id"] for e in selected}
            state["buffer"] = [e for e in state["buffer"] if e["id"] not in selected_ids]
            state["next_flush_at"] = self.clock() if state["buffer"] else None
            state["outbox"] = batch
            return batch

    def begin_send(self, task_id):
        with self.store.edit(task_id) as (_, state):
            batch = state["outbox"]
            batch.update(status="sending", attempts=batch["attempts"] + 1)
            return copy.deepcopy(batch)

    def send_result(self, task_id, batch_id, status, error=None):
        with self.store.edit(task_id) as (spec, state):
            batch = state["outbox"]
            if state["terminal"] or not batch or batch["id"] != batch_id:
                return
            if status in {"accepted", "queued"}:
                state.update(outbox=None, last_sent_at=self.clock())
                self._finish_if_drained(state)
            else:
                batch.update(status="rejected" if status == "rejected" else "ambiguous", error=(error or status)[:1000],
                             next_attempt_at=self.clock() + spec["retry_seconds"])

    def resolve(self, task_id, outcome):
        with self.store.edit(task_id) as (_, state):
            batch = state["outbox"]
            if not batch or batch["status"] not in {"ambiguous", "rejected"}:
                raise ValueError("only an uncertain/rejected batch can be resolved")
            if outcome == "accepted":
                state.update(outbox=None, last_sent_at=self.clock())
                self._finish_if_drained(state)
            elif outcome == "not-delivered":
                batch.update(status="ready", attempts=0, next_attempt_at=self.clock(), error=None)
            else:
                raise ValueError("outcome must be accepted or not-delivered after checking the actual delivery")
        return self.show(task_id)

    def cleanup(self, retention=300):
        for spec, state in self.store.all():
            if state["terminal_at"] is not None and self.clock() - state["terminal_at"] >= retention:
                self.store.delete(spec["id"])
