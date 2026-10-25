import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_task_manager.common import stamp, validate_spec
from codex_task_manager.manager import Manager
from codex_task_manager.storage import Store


OWNER = {"peer_id": "test-peer-a", "thread_id": "00000000-0000-4000-8000-000000000001", "label": "agent-a"}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1800000000.0
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.m = Manager(self.store, lambda: self.now)

    def task(self, **kwargs):
        spec = {"name": "comparison", "owner": OWNER, "cwd": str(self.root), "mode": "repeat", "window_seconds": 10, **kwargs}
        return self.m.submit(spec)["id"]

    def event(self, identity, **kwargs):
        return {"id": identity, "text": f"Result {identity} needs attention", **kwargs}

    def send_ok(self, task_id):
        self.m.freeze(task_id)
        batch = self.m.begin_send(task_id)
        self.m.send_result(task_id, batch["id"], "accepted")
        return batch

    def test_fixed_window_is_not_extended_by_later_events(self):
        task = self.task()
        self.m.emit(task, [self.event("a")])
        self.now += 9
        self.m.emit(task, [self.event("b")])
        self.assertIsNone(self.m.freeze(task))
        self.now += 1
        batch = self.m.freeze(task)
        self.assertEqual({e["id"] for e in batch["events"]}, {"a", "b"})

    def test_next_action_is_available_in_status_and_direct_notification(self):
        task = self.task(window_seconds=0, next_action="Compare A/B metrics and diagnose pending C")
        self.m.emit(task, [self.event("a")])
        self.assertEqual(self.m.show(task)["next_action"], "Compare A/B metrics and diagnose pending C")
        self.assertEqual(self.m.list()[0]["next_action"], "Compare A/B metrics and diagnose pending C")
        batch = self.m.freeze(task)
        self.assertIn("Next action: Compare A/B metrics and diagnose pending C", batch["text"])

    def test_description_is_persisted_and_editing_preserves_observation_state(self):
        old_task = self.task()
        self.assertIsNone(self.m.show(old_task)["description"])
        task = self.task(description="  Compare background evaluations  ")
        self.m.emit(task, [self.event("a")])
        before = self.store.get(task)[1]
        self.assertEqual(self.m.show(task)["description"], "Compare background evaluations")
        self.m.describe(task, "观察 A/B 是否具备比较条件")
        self.assertEqual(self.store.get(task)[1], before)
        self.assertEqual(self.m.show(task)["description"], "观察 A/B 是否具备比较条件")
        for invalid in (None, "", "  ", 42, "x"*1001):
            with self.assertRaisesRegex(ValueError, "description"):
                self.m.describe(task, invalid)
        other = Store(self.root)
        try:
            self.assertEqual(Manager(other).show(task)["description"], "观察 A/B 是否具备比较条件")
        finally:
            other.close()
        self.assertEqual(self.store.get(task)[1], before)

    def test_dedup_ignores_poll_timestamps_but_detects_changed_facts(self):
        task = self.task(window_seconds=0)
        self.m.emit(task, [self.event("a", at=stamp(self.now))])
        self.send_ok(task)
        self.now += 30
        self.assertEqual(self.m.emit(task, [self.event("a", at=stamp(self.now))])["added"], 0)
        with self.assertRaisesRegex(ValueError, "changed facts"):
            self.m.emit(task, [self.event("a", text="Changed fact")])
        self.assertEqual(self.m.show(task)["buffered"], 0)

    def test_sending_snapshot_does_not_clear_new_arrivals(self):
        task = self.task(window_seconds=0)
        self.m.emit(task, [self.event("a")])
        self.m.freeze(task)
        first = self.m.begin_send(task)
        self.m.emit(task, [self.event("b")])
        self.m.send_result(task, first["id"], "accepted")
        self.assertEqual(self.m.show(task)["buffered"], 1)
        second = self.send_ok(task)
        self.assertEqual([e["id"] for e in second["events"]], ["b"])

    def test_one_queue_rejection_does_not_block_another_owner(self):
        a = self.task(window_seconds=0)
        other = {**OWNER, "thread_id": str(uuid.uuid4()), "peer_id": "test-peer-b"}
        b = self.task(window_seconds=0, owner=other)
        self.m.emit(a, [self.event("a")])
        self.m.freeze(a)
        batch = self.m.begin_send(a)
        self.m.send_result(a, batch["id"], "rejected")
        self.m.emit(b, [self.event("b")])
        self.send_ok(b)
        self.assertEqual(self.m.show(a)["status"], "delivery_rejected")
        self.assertIsNotNone(self.m.show(b)["last_sent_at"])
        self.assertEqual(len(self.m.list(owner=other["thread_id"])), 1)

    def test_importance_marks_without_forcing_send_and_immediate_flushes(self):
        task = self.task()
        self.m.emit(task, [self.event("a", important=True)])
        self.assertIsNone(self.m.freeze(task))
        self.m.emit(task, [self.event("b", immediate=True)])
        batch = self.m.freeze(task)
        self.assertIn("[IMPORTANT]", batch["text"])
        self.assertEqual(len(batch["events"]), 2)

    def test_progress_replacement_preserves_other_member_results(self):
        task = self.task()
        self.m.emit(task, [self.event("progress-1", replace_key="download"), self.event("member-a")])
        self.m.emit(task, [self.event("progress-2", replace_key="download")])
        _, state = self.store.get(task)
        self.assertEqual({e["id"] for e in state["buffer"]}, {"progress-2", "member-a"})

    def test_late_progress_does_not_replace_newer_snapshot(self):
        task = self.task()
        self.m.emit(task, [self.event("new", replace_key="progress", at=stamp(self.now + 10))])
        self.m.emit(task, [self.event("old", replace_key="progress", at=stamp(self.now))])
        self.assertEqual([e["id"] for e in self.m.show(task, True)["buffer"]], ["new"])

    def test_capacity_rejection_preserves_already_accepted_buffer(self):
        task = self.task(max_buffer_bytes=4096)
        self.m.emit(task, [self.event("accepted", text="A" * 2000)])
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.m.emit(task, [self.event("overflow", text="B" * 2000)])
        self.assertEqual([e["id"] for e in self.m.show(task, True)["buffer"]], ["accepted"])

    def test_scheduled_timer_wins_over_same_instant_wait_review(self):
        task = self.task(mode="once", fire_at=stamp(self.now + 1), review_interval_seconds=1, message="Inspect the comparison report")
        self.now += 1
        self.m.tick()
        events = self.m.show(task, True)["buffer"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "Inspect the comparison report")

    def test_timestamp_order_and_text_budget_split_without_dropping_events(self):
        task = self.task(window_seconds=0, max_message_chars=800)
        self.m.emit(task, [self.event("late", text="L" * 300, at=stamp(self.now + 1)),
                           self.event("early", text="E" * 300, at=stamp(self.now))])
        ids = []
        while self.m.show(task)["buffered"]:
            batch = self.send_ok(task)
            self.assertLessEqual(len(batch["text"]), 800)
            ids.extend(e["id"] for e in batch["events"])
        self.assertEqual(set(ids), {"early", "late"})
        self.assertEqual(len(ids), 2)

    def test_success_discards_message_body_and_keeps_bounded_dedup(self):
        task = self.task(window_seconds=0, seen_limit=2, max_events=2)
        for key in ("a", "b", "c"):
            self.m.emit(task, [self.event(key)])
            self.send_ok(task)
        _, state = self.store.get(task)
        self.assertEqual(state["buffer"], [])
        self.assertIsNone(state["outbox"])
        self.assertEqual(len(state["seen"]), 2)
        self.assertNotIn("Result", json.dumps(state))

    def test_once_finishes_and_rejects_late_producer_then_is_cleaned(self):
        task = self.task(mode="once", window_seconds=0)
        self.m.emit(task, [self.event("a")])
        self.send_ok(task)
        self.assertEqual(self.m.show(task)["status"], "finished")
        with self.assertRaisesRegex(ValueError, "ended"):
            self.m.emit(task, [self.event("b")])
        self.now += 301
        self.m.cleanup()
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.m.show(task)

    def test_cancellation_drops_pending_and_ignores_inflight_receipt(self):
        task = self.task(window_seconds=0)
        self.m.emit(task, [self.event("a")])
        self.m.freeze(task)
        batch = self.m.begin_send(task)
        self.m.cancel(task)
        self.m.send_result(task, batch["id"], "accepted")
        view = self.m.show(task)
        self.assertEqual((view["status"], view["buffered"], view["sending_events"]), ("cancelled", 0, 0))

    def test_pending_reviews_repeat_by_interval_without_repeating_each_poll(self):
        task = self.task(review_interval_seconds=60)
        self.m.tick()
        self.assertEqual(self.m.show(task)["buffered"], 0)
        self.now += 60
        self.m.tick()
        batch = self.send_ok(task)
        self.assertEqual(batch["events"][0]["kind"], "wait_review")
        for _ in range(3):
            self.m.tick()
        self.assertEqual(self.m.show(task)["buffered"], 0)
        self.now += 60
        self.m.tick()
        self.send_ok(task)
        self.assertEqual(self.m.show(task)["review_count"], 2)
        self.m.configure_review(task, 30)
        self.now += 30
        self.m.tick()
        self.assertEqual(self.m.show(task)["buffered"], 1)
        self.assertEqual(self.m.show(task)["review_count"], 3)
        self.assertEqual(self.m.show(task)["next_review_at"], stamp(self.now + 30))
        self.m.configure_review(task, None)
        self.assertIsNone(self.m.show(task)["next_review_at"])
        self.assertEqual(self.m.show(task)["buffered"], 0)

    def test_review_does_not_end_once_observation_and_completion_stops_reviews(self):
        task = self.task(mode="once", review_interval_seconds=10)
        self.now += 10
        self.m.tick()
        self.send_ok(task)
        self.assertFalse(self.m.show(task)["source_done"])
        self.assertEqual(self.m.show(task)["status"], "waiting")
        self.now += 10
        self.m.tick()
        self.send_ok(task)
        self.m.emit(task, [self.event("real-completion", immediate=True)])
        self.send_ok(task)
        self.assertEqual(self.m.show(task)["status"], "finished")
        self.now += 100
        self.m.tick()
        self.assertEqual(self.m.show(task)["buffered"], 0)

    def test_missed_reviews_coalesce_and_keep_only_latest_unsent_review(self):
        task = self.task(review_interval_seconds=10)
        self.now += 1000
        self.m.recover()
        self.m.tick()
        self.assertEqual(self.m.show(task)["review_count"], 1)
        self.assertEqual(self.m.show(task)["next_review_at"], stamp(self.now + 10))
        self.now += 10
        self.m.tick()
        self.assertEqual(self.m.show(task)["review_count"], 2)
        self.assertEqual(self.m.show(task)["buffered"], 1)

    def test_real_completion_removes_obsolete_unsent_review(self):
        task = self.task(mode="once", review_interval_seconds=10)
        self.now += 10
        self.m.tick()
        self.assertEqual(self.m.show(task)["buffered"], 1)
        self.m.emit(task, [self.event("real-completion")])
        self.assertEqual([e["id"] for e in self.m.show(task, True)["buffer"]], ["real-completion"])
        self.assertIsNone(self.m.show(task)["next_review_at"])

    def test_early_version_task_state_migrates_without_losing_deadline(self):
        task = self.task()
        with self.store.edit(task) as (_, state):
            for key in ("next_review_at", "last_review_at", "review_interval_seconds", "review_count"):
                state.pop(key)
            state.update(review_at=self.now + 60, review_fired=False)
        self.m.recover()
        view = self.m.show(task)
        self.assertEqual(view["review_interval_seconds"], 60)
        self.assertEqual(view["next_review_at"], stamp(self.now + 60))

    def test_due_check_completes_before_review_and_pending_check_gets_review(self):
        complete = self.task(mode="once", check=["true"], review_interval_seconds=1)
        self.now += 1
        self.m.tick()
        self.assertEqual(self.m.show(complete)["buffered"], 0)
        self.m.begin_check(complete)
        self.m.check_result(complete, {"events": [self.event("ready")], "done": True, "status": "completed"})
        self.m.tick()
        self.assertEqual([e["id"] for e in self.m.show(complete, True)["buffer"]], ["ready"])
        pending = self.task(check=["true"], interval_seconds=0.05, review_interval_seconds=1)
        self.now += 1
        self.m.begin_check(pending)
        self.m.check_result(pending, {"events": [], "status": "pending"})
        self.now += 1
        self.m.tick()
        self.assertEqual(self.m.show(pending, True)["buffer"][0]["kind"], "wait_review")

    def test_error_episode_is_visible_and_not_an_alert_every_poll(self):
        task = self.task(check=["true"], error_notify_after=2)
        for _ in range(5):
            self.m.check_result(task, error="remote status unavailable")
        view = self.m.show(task)
        self.assertEqual(view["consecutive_errors"], 5)
        self.assertEqual(view["buffered"], 1)
        self.m.check_result(task, {"events": [], "status": "running"})
        self.assertEqual(self.m.show(task)["consecutive_errors"], 0)

    def test_bad_multi_event_result_rolls_back_instead_of_publishing_partial_data(self):
        task = self.task(check=["true"])
        self.m.check_result(task, {"events": [self.event("a"), {"id": "bad"}]})
        self.assertEqual(self.m.show(task)["buffered"], 0)
        self.assertEqual(self.m.show(task)["consecutive_errors"], 1)

    def test_restart_preserves_unsent_buffer_and_freezes_uncertain_send(self):
        a = self.task()
        b = self.task(window_seconds=0)
        self.m.emit(a, [self.event("a")])
        self.m.emit(b, [self.event("b")])
        self.m.freeze(b)
        self.m.begin_send(b)
        self.m.recover()
        self.assertEqual(self.m.show(a)["buffered"], 1)
        self.assertEqual(self.m.show(b)["status"], "delivery_ambiguous")
        self.m.resolve(b, "accepted")
        self.assertIsNone(self.m.show(b)["delivery"])

    def test_restart_does_not_blindly_rerun_interrupted_script(self):
        task = self.task(check=["true"])
        self.m.begin_check(task)
        self.m.recover()
        self.assertEqual(self.m.show(task)["status"], "paused")
        self.m.resume(task)
        self.assertEqual(self.m.show(task)["status"], "waiting")

    def test_timer_is_once_or_repeat_without_replaying_missed_ticks(self):
        once = self.task(mode="once", fire_at=stamp(self.now + 10))
        repeat = self.task(interval_seconds=10)
        self.now += 100
        self.m.tick()
        self.assertEqual(self.m.show(once)["buffered"], 1)
        self.assertEqual(self.m.show(repeat)["buffered"], 1)
        self.assertEqual(self.m.show(repeat)["next_check_at"], stamp(self.now + 10))

    def test_submission_is_idempotent_for_explicit_id(self):
        task = self.task()
        spec, _ = self.store.get(task)
        self.assertEqual(self.m.submit(spec)["id"], task)
        with self.assertRaisesRegex(ValueError, "different"):
            self.m.submit({**spec, "name": "changed"})

    def test_invalid_configuration_does_not_register(self):
        for values in ({"review_interval_seconds": 0}, {"interval_seconds": 0},
                       {"check": "sh command"}, {"mode": "always"}, {"unknown": True}):
            with self.subTest(values=values), self.assertRaises((ValueError, TypeError)):
                self.task(**values)
        self.assertEqual(self.store.all(), [])


if __name__ == "__main__":
    unittest.main()
