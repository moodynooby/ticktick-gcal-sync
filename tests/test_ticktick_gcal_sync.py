from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

import ticktick_gcal_sync as sync


class TaskToEventTests(unittest.TestCase):
    def test_allday_end_date_is_exclusive_so_add_one_day(self) -> None:
        event, _ = sync.task_to_event(
            {
                "id": "a",
                "title": "Holiday",
                "dueDate": "2026-10-01T00:00:00+0000",
                "isAllDay": True,
            },
            "Personal",
        )
        assert event is not None
        self.assertEqual(event["start"], {"date": "2026-10-01"})
        # Google Calendar treats an all-day `end.date` as exclusive: same-day
        # start/end renders a zero-length, invisible event.
        self.assertEqual(event["end"], {"date": "2026-10-02"})

    def test_timed_task_without_duration_defaults_to_one_hour(self) -> None:
        event, _ = sync.task_to_event(
            {
                "id": "b",
                "title": "Call",
                "startDate": "2026-10-01T09:00:00+0000",
                "dueDate": "2026-10-01T09:00:00+0000",
                "timeZone": "Asia/Kolkata",
            },
            "",
        )
        assert event is not None
        self.assertEqual(
            event["end"]["dateTime"],
            datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc).isoformat(),
        )

    def test_task_without_any_date_is_not_synced(self) -> None:
        self.assertEqual(sync.task_to_event({"id": "c", "title": "x"}, ""), (None, None))


class SyncDaysPastTests(unittest.TestCase):
    """The README documents SYNC_DAYS_PAST; the constant must do something."""

    def test_window_boundaries(self) -> None:
        cases = [
            ("2026-08-15T09:00:00+0000", True),  # older than 30d window
            ("2026-09-20T09:00:00+0000", False),  # inside window
            ("2026-09-03T09:00:00+0000", False),  # boundary day kept
        ]
        for due, expected in cases:
            with self.subTest(due=due):
                self.assertEqual(
                    sync.is_too_old(
                        {"id": "d", "dueDate": due}, date(2026, 10, 3), days_past=30
                    ),
                    expected,
                )

    def test_task_without_due_date_is_not_classified_stale(self) -> None:
        task = {"id": "g", "startDate": "2020-01-01T09:00:00+0000"}
        self.assertFalse(sync.is_too_old(task, date(2026, 10, 3), days_past=30))


if __name__ == "__main__":
    unittest.main()