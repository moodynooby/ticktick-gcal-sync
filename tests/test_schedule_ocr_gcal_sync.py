from __future__ import annotations

import unittest

import schedule_ocr_gcal_sync as importer


class ScheduleOcrTests(unittest.TestCase):
    def test_parses_dmy_date_range(self) -> None:
        parsed = importer.parse_date_range(
            "[03-08-2026 to 22-11-2026] [Room 101]", "DMY"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[0].isoformat(), "2026-08-03")
        self.assertEqual(parsed[1].isoformat(), "2026-11-22")

    def test_parses_time_range(self) -> None:
        self.assertEqual(importer.parse_time_range("08:00 to\n09:30"), (480, 570))
        self.assertIsNone(
            importer.parse_time_range("18:00 to 19:00 20:00 to 21:00")
        )

    def test_splits_and_parses_multiple_lessons_in_one_cell(self) -> None:
        lines = (
            "ENR211 Statistics for Engineers",
            "Section 2",
            "[03-08-2026 to 18-09-2026] [236, GICT Building, Central Campus]",
            "ENR110 Differential Equations in Engineering",
            "Section 3",
            "[05-10-2026 to 22-11-2026] [No Class Room, No Classroom]",
        )
        chunks = importer.split_lesson_chunks(lines)
        self.assertEqual(len(chunks), 2)
        row = importer.TimeRow(600, 828, 600, 828, "\n".join(lines))
        first, first_errors = importer.parse_lesson(
            chunks[0], "MONDAY", row, 95.0, "\n".join(lines), "DMY"
        )
        second, second_errors = importer.parse_lesson(
            chunks[1], "MONDAY", row, 95.0, "\n".join(lines), "DMY"
        )
        self.assertEqual(first_errors, [])
        self.assertEqual(second_errors, [])
        self.assertEqual(first.section, "2")
        self.assertEqual(second.section, "3")
        self.assertEqual(first.location, "236, GICT Building, Central Campus")
        self.assertEqual(second.location, "No Class Room, No Classroom")
        self.assertEqual(second.starts_on, "2026-10-05")

    def test_builds_timezone_aware_weekly_event(self) -> None:
        lesson = importer.Lesson(
            day="MONDAY",
            start="15:00",
            end="16:00",
            name="ENR206 — Sensors, Instruments and Experimentation",
            code="ENR206",
            title="Sensors, Instruments and Experimentation",
            section="1",
            starts_on="2026-08-03",
            ends_on="2026-11-22",
            location="108 Embedded Systems & VLSI Laboratory",
            ocr_confidence=95.0,
            raw_ocr="ENR206",
        )
        event_id, event_key, body = importer.event_payload(
            lesson, "Asia/Kolkata", color_id="7"
        )
        self.assertTrue(event_id.startswith("timetable"))
        self.assertRegex(event_id, r"^[a-v0-9]+$")
        self.assertEqual(len(event_key), 40)
        self.assertEqual(body["start"]["dateTime"], "2026-08-03T15:00:00+05:30")
        self.assertEqual(body["end"]["dateTime"], "2026-08-03T16:00:00+05:30")
        self.assertEqual(body["colorId"], "7")
        self.assertEqual(
            body["recurrence"],
            ["RRULE:FREQ=WEEKLY;UNTIL=20261122T182959Z"],
        )
        self.assertEqual(
            body["extendedProperties"]["private"]["eventKey"], event_key
        )

    def test_same_subject_gets_same_color(self) -> None:
        first = importer.Lesson(
            day="MONDAY",
            start="10:00",
            end="11:00",
            name="CSE305 — Data Structures",
            code="CSE305",
            title="Data Structures",
            section="1",
            starts_on="2026-08-03",
            ends_on="2026-11-22",
            location="Room 207",
            ocr_confidence=95.0,
            raw_ocr="",
        )
        second = importer.Lesson(
            day="WEDNESDAY",
            start="11:00",
            end="12:00",
            name="CSE305 — Data Structures",
            code="CSE305",
            title=" data   structures ",
            section="1",
            starts_on="2026-08-03",
            ends_on="2026-11-22",
            location="Room 207",
            ocr_confidence=95.0,
            raw_ocr="",
        )
        other = importer.Lesson(
            day="MONDAY",
            start="10:00",
            end="11:00",
            name="CSE213 — Digital Logic",
            code="CSE213",
            title="Digital Logic",
            section="1",
            starts_on="2026-08-03",
            ends_on="2026-11-22",
            location="Room 135",
            ocr_confidence=95.0,
            raw_ocr="",
        )
        self.assertEqual(importer.subject_color(first), importer.subject_color(second))
        self.assertNotEqual(importer.subject_color(first), importer.subject_color(other))

    def test_resolves_exact_google_palette_ids(self) -> None:
        class FakeExecutable:
            def __init__(self, value: dict[str, dict[str, str]]) -> None:
                self.value = value

            def execute(self) -> dict[str, dict[str, dict[str, str]]]:
                return {"event": self.value}

        class FakeColors:
            def __init__(self, value: dict[str, dict[str, str]]) -> None:
                self.executable = FakeExecutable(value)

            def get(self) -> FakeExecutable:
                return self.executable

        class FakeService:
            def colors(self) -> FakeColors:
                return FakeColors(
                    {
                        "2": {"background": "#7ae7bf"},
                        "3": {"background": "#dbadff"},
                        "4": {"background": "#ff887c"},
                        "6": {"background": "#ffb878"},
                        "7": {"background": "#46d6db"},
                        "9": {"background": "#5484ed"},
                        "10": {"background": "#51b749"},
                        "11": {"background": "#dc2127"},
                    }
                )

        resolved = importer.resolve_event_color_ids(FakeService())
        self.assertEqual(resolved["#7ae7bf"], "2")
        self.assertEqual(resolved["#5484ed"], "9")
        self.assertEqual(resolved["#dc2127"], "11")
        self.assertEqual(len(set(resolved.values())), 8)

    def test_location_is_required(self) -> None:
        lesson = importer.Lesson(
            day="TUESDAY",
            start="08:00",
            end="09:30",
            name="ENR207 — Electric and Magnetic circuits",
            code="ENR207",
            title="Electric and Magnetic circuits",
            section="1",
            starts_on="2026-08-03",
            ends_on="2026-11-22",
            location="",
            ocr_confidence=94.0,
            raw_ocr="",
        )
        self.assertIn("Event location is empty.", importer.validate_lesson(lesson))

    def test_incomplete_preview_lesson_reaches_validation(self) -> None:
        lesson = importer.Lesson.from_dict(
            {
                "day": "MONDAY",
                "start": "10:00",
                "end": "11:00",
                "name": "Example",
                "code": "",
                "title": "Example",
                "section": "1",
                "starts_on": "",
                "ends_on": "",
                "location": "",
                "ocr_confidence": 80.0,
                "raw_ocr": "Example",
            }
        )
        errors = importer.validate_lesson(lesson)
        self.assertIn("Event location is empty.", errors)
        self.assertIn("Course date range is invalid.", errors)


if __name__ == "__main__":
    unittest.main()
