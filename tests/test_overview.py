import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from codex_task_manager import dashboard, overview


class OverviewTests(unittest.TestCase):
    def snapshot(self):
        return {
            "state_dir": "/tmp/ctm", "repowire_endpoint": "http://localhost:8377",
            "tasks": {"data": [{"id": "same-id", "name": "Comparison", "description": "Compare completed evaluations",
                "status": "buffering", "mode": "repeat", "buffered": 2, "owner": {"peer_id": "owner"},
                "target": {"peer_id": "target"}, "next_check_at": "2026-10-27T12:00:00Z",
                "next_flush_at": "2026-10-27T11:00:00Z", "next_review_at": "2026-10-27T15:00:00Z"}], "error": None},
            "peers": {"data": [{"peer_id": "owner", "display_name": "Owner agent", "session_name": "My session", "status": "online"},
                               {"peer_id": "target", "display_name": "Hidden agent", "session_name": "Target session", "status": "offline", "addressable": False}], "error": None},
            "schedules": {"data": overview.schedules({"schedules": [{"schedule_id": "same-id", "from_peer": "owner", "to_peer": "target",
                "text": "[event-driven-wakeup: id]\n[sent_at: date]\n夜间检查\nRead results", "kind": "notify", "cron": "0 12 * * *",
                "fire_at": "2026-10-27T12:30:00+02:00", "auth_token": "never-show-secret"}]}), "error": None},
            "recurring_jobs": {"data": overview.recurring_jobs({"work": [{"title": "Old occurrence"}], "recurring": [
                {"calendar_id": "same-id", "title": "Daily job", "state": "active", "cron": "0 13 * * *",
                 "next_due_at": "2026-10-27T13:00:00Z", "last_materialized_at": "2026-10-26T13:00:00Z", "owner_peer_id": "owner", "assigned_peer_id": "target"},
                {"calendar_id": "paused", "title": "Paused job", "state": "paused", "cron": "0 1 * * *"},
                {"calendar_id": "cancelled", "title": "Cancelled job", "state": "cancelled", "cron": "0 2 * * *"}]}), "error": None}}

    def test_union_keeps_namespaced_ids_and_the_backends_own_state(self):
        value = self.snapshot()
        before = copy.deepcopy(value)
        rows = dashboard.rows_for(value, "Tasks")
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({dashboard.row_id(row) for row in rows}), 4)
        schedule, ctm, job, paused = rows
        self.assertEqual(schedule["source"], "RW schedule")
        self.assertEqual(schedule["name"], "夜间检查")
        self.assertEqual(schedule["owner_name"], "My session")
        self.assertEqual(schedule["target_name"], "Target session")
        self.assertEqual(schedule["target_state"], "offline")
        self.assertEqual(schedule["status"], "scheduled")
        self.assertIsNone(schedule["last_sent_at"])
        self.assertIsNone(schedule["buffered"])
        self.assertIsNone(schedule["next_review_at"])
        self.assertEqual(schedule["management"]["cancel"], ["repowire", "schedule", "delete", "same-id"])
        self.assertEqual(job["management"]["cancel"], ["repowire", "jobs", "cancel", "same-id"])
        self.assertNotIn("last_sent_at", job)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(ctm["next_kind"], "flush")
        self.assertEqual(ctm["buffered"], 2)
        self.assertEqual(value, before)

    def test_source_and_lifecycle_filters_do_not_follow_agent_visibility(self):
        value = self.snapshot()
        self.assertEqual(len(dashboard.rows_for(value, "Agents")), 1)
        self.assertEqual(len(dashboard.rows_for(value, "Tasks", source="ctm")), 1)
        self.assertEqual(len(dashboard.rows_for(value, "Tasks", source="repowire")), 3)
        self.assertEqual(len(dashboard.rows_for(value, "Tasks", show_all=True)), 5)
        self.assertEqual(len(dashboard.rows_for(value, "Tasks", "Target session")), 3)
        self.assertEqual(dashboard.rows_for(value, "Tasks", "Cancelled job"), [])
        self.assertEqual(len(dashboard.rows_for(value, "Tasks", "Cancelled job", show_all=True)), 1)

    def test_schedule_advance_and_removal_do_not_invent_a_delivery_receipt_or_history(self):
        value = self.snapshot()
        before = next(r for r in overview.task_overview(value) if r["source"] == "RW schedule")
        value["schedules"]["data"][0]["fire_at"] = "2026-10-28T12:30:00+02:00"
        after = next(r for r in overview.task_overview(value) if r["source"] == "RW schedule")
        self.assertEqual(dashboard.row_id(before), dashboard.row_id(after))
        self.assertNotEqual(before["next_at"], after["next_at"])
        self.assertIsNone(after["last_sent_at"])
        value["schedules"]["data"] = []
        self.assertFalse(any(r["source"] == "RW schedule" for r in overview.task_overview(value)))

    def test_all_time_columns_are_utc_with_dates_and_fit_terminal_widths(self):
        value = self.snapshot()
        self.assertEqual(overview.when("2026-10-27T12:30:00+02:00"), "2026-10-27 10:30")
        self.assertEqual(overview.when("bad timestamp"), "-")
        for width in (55, 70, 90, 100, 120, 140, 159, 160, 200):
            rows = dashboard.rows_for(value, "Tasks", show_all=True)
            headers, sizes, cells = dashboard.table_layout(value, "Tasks", rows, width)
            self.assertLessEqual(sum(sizes)+2*(len(sizes)-1), width)
            self.assertIn("SOURCE", headers)
            self.assertIn("NEXT UTC", headers)
        wide = "\n".join(dashboard.table_lines(value, "Tasks", rows, 200))
        self.assertIn("2026-10-27 10:30", wide)
        self.assertIn("RW schedule", wide)
        self.assertIn("[offline]", wide)
        self.assertNotIn("never-show-secret", wide)

    def test_source_failures_stay_explicit_and_are_scoped_to_the_filter(self):
        value = self.snapshot()
        value["tasks"] = {"data": None, "error": "CTM down"}
        self.assertEqual(len(dashboard.rows_for(value, "Tasks")), 3)
        self.assertEqual(overview.source_errors(value, "repowire"), [])
        self.assertEqual(overview.source_errors(value), ["CTM: CTM down"])
        value["schedules"] = {"data": None, "error": "HTTP 404"}
        self.assertEqual(len(dashboard.rows_for(value, "Tasks")), 2)
        self.assertEqual(overview.source_errors(value, "repowire"), ["RW schedules: HTTP 404"])

    def test_empty_collections_and_invalid_shapes_are_distinguished(self):
        self.assertEqual(overview.schedules({"schedules": None}), [])
        self.assertEqual(overview.recurring_jobs({"recurring": None}), [])
        for bad in ({}, {"schedules": {}}, {"schedules": [None]}, {"schedules": [{}]}, {"schedules": [{"schedule_id": "id", "cron": []}]}):
            with self.assertRaises(ValueError):
                overview.schedules(bad)
