#!/usr/bin/env python3
"""One-time timetable screenshot OCR import into Google Calendar.

The default action only performs OCR and writes a review file. Calendar writes
require the explicit --sync flag and a final interactive SYNC confirmation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pytesseract
from dotenv import load_dotenv
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent
DEFAULT_IMAGE = ROOT / "Screenshot from 2026-09-24 12.22.02@2x.png"
DEFAULT_PREVIEW = ROOT / ".schedule_ocr_preview.json"
DEFAULT_VERIFICATION = ROOT / ".schedule_ocr_verification.json"
DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")
OCR_CONFIG = "--oem 3 --psm 6 -c preserve_interword_spaces=1"
SUBJECT_COLORS = {
    "Blueberry": "#5484ed",
    "Sage": "#7ae7bf",
    "Grape": "#dbadff",
    "Flamingo": "#ff887c",
    "Tangerine": "#ffb878",
    "Peacock": "#46d6db",
    "Basil": "#51b749",
    "Tomato": "#dc2127",
}
SUBJECT_COLOR_RULES = (
    (("electric", "magnetic"), "Grape"),
    (("data", "structures"), "Sage"),
    (("statistics",), "Basil"),
    (("differential",), "Tomato"),
    (("digital", "logic"), "Blueberry"),
    (("managerial",), "Tangerine"),
    (("mechanics",), "Flamingo"),
    (("sensors",), "Peacock"),
)
load_dotenv(ROOT / ".env")

DATE_PATTERN = re.compile(
    r"(?P<first>[0-9O]{1,2})\s*[-/.]\s*(?P<second>[0-9O]{1,2})"
    r"\s*[-/.]\s*(?P<year>[0-9O]{2,4})\s*(?:to|till|until)\s*"
    r"(?P<last>[0-9O]{1,2})\s*[-/.]\s*(?P<last_month>[0-9O]{1,2})"
    r"\s*[-/.]\s*(?P<last_year>[0-9O]{2,4})",
    re.IGNORECASE,
)
TIME_PATTERN = re.compile(
    r"(?P<start_hour>[0-9O]{1,2})\s*[:.]\s*(?P<start_minute>[0-9O]{2})"
    r"\s*(?:to|till|until|–|—|-)\s*"
    r"(?P<end_hour>[0-9O]{1,2})\s*[:.]\s*(?P<end_minute>[0-9O]{2})",
    re.IGNORECASE,
)
SECTION_PATTERN = re.compile(r"\bsection\s*[:\-]?\s*([0-9ivx]+)\b", re.IGNORECASE)
COURSE_PATTERN = re.compile(
    r"^(?P<code>[A-Z]{2,4}\s*[0-9O]{3,4})(?=$|[\s:-])"
    r"(?:\s*[-:]\s*|\s+)?(?P<title>.*)$"
)


def resolve_calendar_id(*candidates: object) -> str:
    """First non-blank candidate, else "primary".

    NOTE: os.getenv's default only applies when the var is unset. An empty
    GCAL_CALENDAR_ID (e.g. unset optional secret in CI) would otherwise reach
    the API as /calendars//events (HTTP 404).
    """
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return "primary"


class ImportFailure(RuntimeError):
    """A safe, user-actionable import failure."""


@dataclass(frozen=True)
class OcrText:
    lines: tuple[str, ...]
    confidence: float
    raw: str


@dataclass(frozen=True)
class TimeRow:
    top: int
    bottom: int
    start_minute: int
    end_minute: int
    raw_ocr: str

    @property
    def start(self) -> str:
        return format_minute(self.start_minute)

    @property
    def end(self) -> str:
        return format_minute(self.end_minute)


@dataclass(frozen=True)
class Review:
    """One OCR pass (or reviewed file) ready to be printed and optionally synced."""

    lessons: list[Lesson]
    timezone_name: str
    calendar_id: str
    warnings: list[str]
    errors: list[str]


@dataclass(frozen=True)
class DayRegion:
    day: str
    left: int
    right: int
    header_ocr: str


@dataclass(frozen=True)
class Lesson:
    day: str
    start: str
    end: str
    name: str
    code: str
    title: str
    section: str
    starts_on: str
    ends_on: str
    location: str
    ocr_confidence: float
    raw_ocr: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["warnings"] = list(self.warnings)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lesson:
        return cls(
            day=str(data.get("day", "")).upper(),
            start=str(data.get("start", "")),
            end=str(data.get("end", "")),
            name=str(data.get("name", "")).strip(),
            code=str(data.get("code", "")).strip(),
            title=str(data.get("title", "")).strip(),
            section=str(data.get("section", "")).strip(),
            starts_on=str(data.get("starts_on", "")),
            ends_on=str(data.get("ends_on", "")),
            location=str(data.get("location", "")).strip(),
            ocr_confidence=float(data.get("ocr_confidence", 0.0)),
            raw_ocr=str(data.get("raw_ocr", "")),
            warnings=tuple(str(item) for item in data.get("warnings", [])),
        )


def format_minute(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_minute(hour_text: str, minute_text: str) -> int:
    hour = int(hour_text.upper().replace("O", "0"))
    minute = int(minute_text.upper().replace("O", "0"))
    if minute >= 60 or hour > 24 or (hour == 24 and minute != 0):
        raise ValueError("invalid clock time")
    return hour * 60 + minute


def parse_hhmm(text: str) -> int:
    """Minutes since midnight for an "HH:MM" string, as stored on a Lesson."""
    return parse_minute(text[:2], text[3:5])


def parse_time_range(text: str) -> tuple[int, int] | None:
    normalized = text.replace("–", "-").replace("—", "-")
    matches = list(TIME_PATTERN.finditer(normalized))
    if len(matches) != 1:
        return None
    match = matches[0]
    try:
        start = parse_minute(match.group("start_hour"), match.group("start_minute"))
        end = parse_minute(match.group("end_hour"), match.group("end_minute"))
    except ValueError:
        return None
    return (start, end) if end > start else None


def parse_date(
    first_text: str,
    second_text: str,
    year_token: str,
    date_order: str,
) -> date:
    first = int(first_text.upper().replace("O", "0"))
    second = int(second_text.upper().replace("O", "0"))
    year = int(year_token.upper().replace("O", "0"))
    if year < 100:
        year += 2000
    if date_order == "MDY":
        month, day = first, second
    elif first > 12 and second <= 12:
        day, month = first, second
    elif second > 12 and first <= 12:
        month, day = first, second
    else:
        day, month = first, second
    return date(year, month, day)


def parse_date_range(text: str, date_order: str) -> tuple[date, date, re.Match[str]] | None:
    matches = list(DATE_PATTERN.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    try:
        start = parse_date(
            match.group("first"),
            match.group("second"),
            match.group("year"),
            date_order,
        )
        end = parse_date(
            match.group("last"),
            match.group("last_month"),
            match.group("last_year"),
            date_order,
        )
    except ValueError:
        return None
    return (start, end, match) if start <= end else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def subject_key(lesson: Lesson) -> str:
    source = lesson.title or lesson.name
    return re.sub(r"[^a-z0-9]+", " ", source.casefold()).strip()


def subject_color(lesson: Lesson) -> tuple[str, str]:
    normalized = subject_key(lesson)
    words = set(normalized.split())
    for keywords, color_name in SUBJECT_COLOR_RULES:
        if all(keyword in words for keyword in keywords):
            return color_name, SUBJECT_COLORS[color_name]
    names = tuple(SUBJECT_COLORS)
    index = int.from_bytes(hashlib.sha256(normalized.encode()).digest()[:2], "big")
    color_name = names[index % len(names)]
    return color_name, SUBJECT_COLORS[color_name]


def preview_lesson_dict(lesson: Lesson) -> dict[str, Any]:
    data = lesson.to_dict()
    color_name, color_hex = subject_color(lesson)
    data["subject_key"] = subject_key(lesson)
    data["color_name"] = color_name
    data["color_hex"] = color_hex
    return data


def line_mask(image: Image.Image) -> np.ndarray:
    pixels = np.asarray(image.convert("RGB"))
    spread = pixels.max(axis=2) - pixels.min(axis=2)
    brightness = pixels.mean(axis=2)
    return (spread <= 6) & (brightness >= 160) & (brightness <= 245)


def runs_from_mask(mask: np.ndarray) -> list[tuple[int, int]]:
    padded = np.concatenate((np.array([False]), mask, np.array([False])))
    changes = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(start), int(end)) for start, end in changes.reshape(-1, 2)]


def detect_grid(image: Image.Image) -> tuple[list[int], list[int]]:
    mask = line_mask(image)
    height, width = mask.shape

    vertical_scores = mask.sum(axis=0)
    vertical_runs = runs_from_mask(vertical_scores > height * 0.72)
    vertical_lines = [
        (start + end - 1) // 2
        for start, end in vertical_runs
        if (start + end - 1) // 2 >= width * 0.02
    ]
    if len(vertical_lines) < 4:
        raise ImportFailure("Could not detect the timetable's vertical grid lines.")

    first_x = vertical_lines[0]
    last_x = vertical_lines[-1]
    horizontal_scores = mask[:, first_x : last_x + 1].sum(axis=1)
    horizontal_runs = runs_from_mask(
        horizontal_scores > (last_x - first_x + 1) * 0.75
    )
    horizontal_lines = [(start + end - 1) // 2 for start, end in horizontal_runs]
    if len(horizontal_lines) < 2:
        raise ImportFailure("Could not detect the timetable's row boundaries.")

    return vertical_lines, horizontal_lines


def preprocess_crop(crop: Image.Image, scale: int = 3) -> Image.Image:
    grayscale = ImageOps.autocontrast(crop.convert("L"))
    resized = grayscale.resize(
        (grayscale.width * scale, grayscale.height * scale),
        Image.Resampling.LANCZOS,
    )
    return resized.filter(ImageFilter.UnsharpMask(radius=1, percent=140, threshold=2))


def ocr_image(crop: Image.Image, language: str, scale: int = 3) -> OcrText:
    data = pytesseract.image_to_data(
        preprocess_crop(crop, scale),
        lang=language,
        config=OCR_CONFIG,
        output_type=pytesseract.Output.DICT,
        timeout=60,
    )
    grouped: dict[tuple[int, int, int], list[tuple[str, float]]] = {}
    for index, word in enumerate(data["text"]):
        text = str(word).strip()
        if not text:
            continue
        key = (
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1.0
        grouped.setdefault(key, []).append((text, confidence))

    lines: list[str] = []
    confidences: list[float] = []
    for words in grouped.values():
        lines.append(" ".join(word for word, _ in words).strip())
        confidences.extend(confidence for _, confidence in words if confidence >= 0)
    return OcrText(
        tuple(line for line in lines if line),
        sum(confidences) / len(confidences) if confidences else 0.0,
        "\n".join(lines),
    )


def cell_has_text(crop: Image.Image) -> bool:
    pixels = np.asarray(crop.convert("L"))
    return int((pixels < 185).sum()) >= 20


def ocr_cell(crop: Image.Image, language: str, scale: int = 3) -> OcrText | None:
    """OCR one timetable cell, or None when the cell is blank."""
    return ocr_image(crop, language, scale) if cell_has_text(crop) else None


def row_intervals(image: Image.Image, horizontal_lines: list[int]) -> list[tuple[int, int]]:
    """Row bands between the detected rules, dropping a trailing rule that sits
    on the image edge (it would yield a zero-height band)."""
    edges = [*horizontal_lines, image.height]
    if edges[-2] >= image.height - 2:
        edges.pop(-2)
    return list(zip(edges, edges[1:]))


def detect_time_rows(
    image: Image.Image,
    vertical_lines: list[int],
    horizontal_lines: list[int],
    language: str,
) -> tuple[list[TimeRow], list[str], list[str]]:
    intervals = row_intervals(image, horizontal_lines)
    time_right = vertical_lines[0] - 2
    cache = [
        ocr_cell(image.crop((2, top + 2, time_right, bottom - 2)), language)
        or OcrText((), 0.0, "")
        for top, bottom in intervals
    ]

    rows: list[TimeRow] = []
    notes: list[str] = []
    errors: list[str] = []
    index = 0
    while index < len(intervals):
        # A time label split across a stray rule needs up to 3 bands to read.
        combined: list[str] = []
        parsed: tuple[int, int] | None = None
        last_index = index
        for candidate in range(index, min(index + 3, len(intervals))):
            combined.extend(cache[candidate].lines)
            parsed = parse_time_range("\n".join(combined))
            if parsed is not None:
                last_index = candidate
                break
        top = intervals[index][0]
        if parsed is None:
            errors.append(
                f"Could not read a complete time range near y={top} ({cache[index].raw!r})."
            )
            index += 1
            continue
        if last_index > index:
            notes.append(
                f"Merged an extra row rule at y={top} into {format_minute(parsed[0])}-{format_minute(parsed[1])}."
            )
        rows.append(
            TimeRow(
                top,
                intervals[last_index][1],
                parsed[0],
                parsed[1],
                "\n".join(combined),
            )
        )
        index = last_index + 1

    if not rows:
        raise ImportFailure("No valid time rows were detected in the timetable.")
    return rows, notes, errors


def detect_day_regions(
    image: Image.Image,
    vertical_lines: list[int],
    header_bottom: int,
    language: str,
) -> tuple[list[DayRegion], list[str]]:
    regions: list[DayRegion] = []
    warnings: list[str] = []
    for index, day in enumerate(DAYS):
        if index >= len(vertical_lines):
            break
        left = vertical_lines[index]
        right = vertical_lines[index + 1] if index + 1 < len(vertical_lines) else image.width
        if right - left < 8:
            continue
        header = ocr_image(
            image.crop((left + 2, 1, right - 2, max(2, header_bottom - 1))),
            language,
            scale=4,
        )
        compact = re.sub(r"[^A-Z]", "", header.raw.upper())
        if day[:3] not in compact:
            warnings.append(
                f"Header OCR for {day.title()} was {header.raw!r}; the script used the visible column order."
            )
        if right - left < 80:
            warnings.append(
                f"The {day.title()} column is cropped to {right - left}px and cannot be OCR-read safely; it was skipped."
            )
        regions.append(DayRegion(day, left, right, header.raw))
    if not regions:
        raise ImportFailure("No weekday columns were detected.")
    return regions, warnings


def split_course(line: str) -> tuple[str, str] | None:
    match = COURSE_PATTERN.match(line.strip())
    if not match:
        return None
    code = re.sub(r"\s+", "", match.group("code").upper())
    return code, match.group("title").strip()


def split_lesson_chunks(lines: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Split one cell's OCR lines into per-course chunks.

    Each chunk starts at a course code and runs to the next one, so a single
    time slot holding two courses yields two lessons.
    """
    starts = [index for index, line in enumerate(lines) if split_course(line)]
    bounds = [*starts, len(lines)]
    return [lines[start:end] for start, end in zip(bounds, bounds[1:])] or [lines]


def clean_location(text: str) -> str:
    location = " ".join(part.strip() for part in text.splitlines() if part.strip())
    location = re.sub(r"^[\s\[\]]+", "", location)
    location = re.sub(r"[\s\[\]]+$", "", location)
    return re.sub(r"\s+", " ", location).strip(" ,;")


def split_code_and_title(chunk: tuple[str, ...]) -> tuple[str, list[str]]:
    """Pull the course code off the first matching line.

    Returns the code and the remaining lines, one entry per input line, so
    indices still line up with the chunk when the title is sliced later.
    """
    code = ""
    titles: list[str] = []
    for line in chunk:
        parsed_code = split_course(line)
        if parsed_code and not code:
            code, remainder = parsed_code
            titles.append(remainder)
        else:
            titles.append(line)
    return code, titles


def parse_lesson(
    chunk: tuple[str, ...],
    day: str,
    row: TimeRow,
    confidence: float,
    raw_ocr: str,
    date_order: str,
) -> tuple[Lesson, list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    code, title_lines = split_code_and_title(chunk)

    section_match = SECTION_PATTERN.search("\n".join(chunk))
    section = section_match.group(1).upper() if section_match else ""
    if not section:
        warnings.append("Section number was not recognized.")

    parsed_dates: tuple[date, date, re.Match[str]] | None = None
    date_index = -1
    for index, line in enumerate(chunk):
        candidate = parse_date_range(line, date_order)
        if candidate:
            parsed_dates = candidate
            date_index = index
            break
    if parsed_dates is None:
        errors.append("Date range was not recognized.")
        starts_on = ends_on = ""
        location = ""
    else:
        starts_on_date, ends_on_date, date_match = parsed_dates
        starts_on = starts_on_date.isoformat()
        ends_on = ends_on_date.isoformat()
        location_parts = [chunk[date_index][date_match.end() :], *chunk[date_index + 1 :]]
        location = clean_location("\n".join(location_parts))
        if not location:
            errors.append("Location was not recognized.")

    # The title is everything before the section/date tail of the chunk.
    section_index = next(
        (index for index, line in enumerate(chunk) if SECTION_PATTERN.search(line)),
        date_index if date_index >= 0 else len(chunk),
    )
    title = " ".join(
        line
        for line in title_lines[:section_index]
        if line and not SECTION_PATTERN.search(line) and not DATE_PATTERN.search(line)
    )
    title = re.sub(r"\s+", " ", title).strip(" -:")
    if not title:
        errors.append("Course name was not recognized.")
    if not code:
        warnings.append("Course code was not recognized; verify the name.")
    elif not re.fullmatch(r"[A-Z]{2,4}\d{3,4}", code):
        warnings.append(f"Course code {code!r} may contain an OCR letter/digit error.")
    if confidence < 70:
        warnings.append(f"Low OCR confidence ({confidence:.1f}%).")

    name = f"{code} — {title}" if code else title
    lesson = Lesson(
        day=day,
        start=row.start,
        end=row.end,
        name=name,
        code=code,
        title=title,
        section=section,
        starts_on=starts_on,
        ends_on=ends_on,
        location=location,
        ocr_confidence=round(confidence, 1),
        raw_ocr=raw_ocr,
        warnings=tuple(warnings),
    )
    errors.extend(validate_lesson(lesson))
    return lesson, list(dict.fromkeys(errors))


def validate_lesson(lesson: Lesson) -> list[str]:
    errors: list[str] = []
    try:
        start_minute = parse_hhmm(lesson.start)
        end_minute = parse_hhmm(lesson.end)
        if end_minute <= start_minute:
            raise ValueError
    except (ValueError, IndexError):
        errors.append("Start/end time is invalid.")
    if lesson.day not in DAYS:
        errors.append(f"Unknown weekday {lesson.day!r}.")
    if not lesson.name.strip():
        errors.append("Event name is empty.")
    if not lesson.location.strip():
        errors.append("Event location is empty.")
    try:
        if date.fromisoformat(lesson.starts_on) > date.fromisoformat(lesson.ends_on):
            errors.append("Course start date is after its end date.")
    except ValueError:
        errors.append("Course date range is invalid.")
    return list(dict.fromkeys(errors))


def open_image(image_path: Path) -> tuple[Image.Image, str]:
    """Return the RGB screenshot plus its Tesseract version banner."""
    version = str(pytesseract.get_tesseract_version()).splitlines()[0]
    try:
        with Image.open(image_path) as opened:
            return opened.convert("RGB"), version
    except FileNotFoundError as error:
        raise ImportFailure(f"Screenshot not found: {image_path}") from error
    except UnidentifiedImageError as error:
        raise ImportFailure(f"Not a readable image: {image_path}") from error


def extract_preview(
    image_path: Path,
    preview_path: Path,
    language: str,
    date_order: str,
    timezone_name: str,
    calendar_id: str,
    tesseract_cmd: str | None,
) -> dict[str, Any]:
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        image, tesseract_version = open_image(image_path)
    except pytesseract.TesseractNotFoundError as error:
        raise ImportFailure(
            "Tesseract is not installed. On Ubuntu run: sudo apt install tesseract-ocr tesseract-ocr-eng"
        ) from error
    except pytesseract.TesseractError as error:
        raise ImportFailure(f"Tesseract could not start: {error}") from error

    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ImportFailure(f"Unknown IANA timezone {timezone_name!r}.") from error

    vertical_lines, horizontal_lines = detect_grid(image)
    rows, time_notes, errors = detect_time_rows(
        image, vertical_lines, horizontal_lines, language
    )
    regions, warnings = detect_day_regions(
        image, vertical_lines, horizontal_lines[0], language
    )
    warnings.extend(time_notes)

    lessons: list[Lesson] = []
    for row in rows:
        for region in regions:
            if region.right - region.left < 80:
                continue
            crop = image.crop(
                (
                    region.left + 4,
                    row.top + 3,
                    region.right - 4,
                    row.bottom - 3,
                )
            )
            try:
                result = ocr_cell(crop, language)
            except (pytesseract.TesseractError, RuntimeError) as error:
                errors.append(
                    f"OCR failed for {region.day.title()} {row.start}-{row.end}: {error}"
                )
                continue
            if result is None:
                continue
            for chunk in split_lesson_chunks(result.lines):
                lesson, lesson_errors = parse_lesson(
                    chunk,
                    region.day,
                    row,
                    result.confidence,
                    result.raw,
                    date_order,
                )
                lessons.append(lesson)
                errors.extend(
                    f"{region.day.title()} {row.start}-{row.end} {lesson.name or '(unnamed)'}: {error}"
                    for error in lesson_errors
                )

    if not lessons:
        errors.append("No timetable events were recognized.")
    preview = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "image": image_path.name,
            "sha256": sha256_file(image_path),
            "width": image.width,
            "height": image.height,
            "tesseract": tesseract_version,
            "language": language,
        },
        "calendar_id": calendar_id,
        "timezone": timezone_name,
        "date_order": date_order,
        "geometry": {
            "vertical_lines": vertical_lines,
            "horizontal_lines": horizontal_lines,
            "time_rows": [
                {
                    "top": row.top,
                    "bottom": row.bottom,
                    "start": row.start,
                    "end": row.end,
                    "ocr": row.raw_ocr,
                }
                for row in rows
            ],
        },
        "warnings": warnings,
        "errors": list(dict.fromkeys(errors)),
        "lessons": [preview_lesson_dict(lesson) for lesson in lessons],
    }
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_text(json.dumps(preview, indent=2, ensure_ascii=False) + "\n")
    return preview


def load_preview(preview_path: Path) -> tuple[list[Lesson], dict[str, Any]]:
    try:
        data = json.loads(preview_path.read_text())
    except FileNotFoundError as error:
        raise ImportFailure(f"Preview file not found: {preview_path}") from error
    except json.JSONDecodeError as error:
        raise ImportFailure(f"Preview file is invalid JSON: {error}") from error
    raw_lessons = data.get("lessons")
    if not isinstance(raw_lessons, list) or not raw_lessons:
        raise ImportFailure("Preview file contains no lessons.")
    lessons = [
        Lesson.from_dict(item) for item in raw_lessons if isinstance(item, dict)
    ]
    if len(lessons) != len(raw_lessons):
        raise ImportFailure("Every lesson in the preview must be a JSON object.")
    return lessons, data


def print_preview(
    lessons: list[Lesson],
    warnings: list[str],
    errors: list[str],
    preview_path: Path,
) -> None:
    print("\nOCR TIMETABLE PREVIEW — NOTHING HAS BEEN SENT")
    print("=" * 72)
    subject_legend: dict[str, tuple[str, str, str]] = {}
    for index, lesson in enumerate(lessons, start=1):
        dates = f"{lesson.starts_on or '?'} to {lesson.ends_on or '?'}"
        color_name, color_hex = subject_color(lesson)
        subject_legend[subject_key(lesson)] = (lesson.title or lesson.name, color_name, color_hex)
        print(
            f"{index:2}. {lesson.day.title():9} {lesson.start}-{lesson.end}  "
            f"[{color_name}] {lesson.name or '(name missing)'}"
        )
        print(f"    Location: {lesson.location or '(location missing)'}")
        print(
            f"    Color:    {color_name} ({color_hex}); dates: {dates}; "
            f"section: {lesson.section or '?'}; OCR: {lesson.ocr_confidence:.1f}%"
        )
    if subject_legend:
        print("\nSubject color legend:")
        for subject, (_, color_name, color_hex) in sorted(subject_legend.items()):
            print(f"- {subject}: {color_name} ({color_hex})")
    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"- {warning}")
        for lesson in lessons:
            for warning in lesson.warnings:
                print(f"- {lesson.day.title()} {lesson.start}: {warning}")
    if errors:
        print("\nBlocking errors:")
        for error in errors:
            print(f"- {error}")
    print(f"\nReview file: {preview_path}")
    print(f"Recognized events: {len(lessons)}")
    if not errors:
        print("All required fields are present: name, time, date range, and location.")


def google_service(credentials_path: Path, token_path: Path) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/calendar"]
    credentials = None
    token_json = os.getenv("GCAL_TOKEN_JSON", "").strip()
    try:
        if token_json:
            credentials = Credentials.from_authorized_user_info(json.loads(token_json), scopes)
        elif token_path.exists():
            credentials = Credentials.from_authorized_user_file(str(token_path), scopes)
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            if not credentials_path.exists():
                raise ImportFailure(
                    f"Google OAuth credentials not found: {credentials_path}. See README.md."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            credentials = flow.run_local_server(port=0)
            token_path.write_text(credentials.to_json())
            try:
                token_path.chmod(0o600)
            except OSError:
                pass
            print(f"Saved refreshed Google token to {token_path}.")
    except ImportFailure:
        raise
    except Exception as error:
        raise ImportFailure(f"Google authentication failed: {error}") from error
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def first_occurrence(lesson: Lesson, zone: ZoneInfo) -> datetime:
    starts_on = date.fromisoformat(lesson.starts_on)
    target_weekday = DAYS.index(lesson.day)
    first_date = starts_on + timedelta(days=(target_weekday - starts_on.weekday()) % 7)
    if first_date > date.fromisoformat(lesson.ends_on):
        raise ImportFailure(
            f"{lesson.day.title()} has no occurrence between {lesson.starts_on} and {lesson.ends_on}."
        )
    hour, minute = divmod(parse_hhmm(lesson.start), 60)
    return datetime.combine(first_date, time(hour, minute), tzinfo=zone)


def rgb(value: str) -> tuple[int, int, int]:
    value = value.removeprefix("#")
    if len(value) != 6:
        raise ValueError(value)
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def color_distance(wanted: str, definition: dict[str, Any]) -> int:
    wanted_rgb = rgb(wanted)
    actual_rgb = rgb(str(definition.get("background", "#000000")))
    return sum((a - b) ** 2 for a, b in zip(wanted_rgb, actual_rgb))


def resolve_event_color_ids(service: Any) -> dict[str, str]:
    available = service.colors().get().execute().get("event", {})
    if not available:
        raise ImportFailure("Google Calendar returned no writable event colors.")

    result: dict[str, str] = {}
    for wanted in set(SUBJECT_COLORS.values()):
        wanted_key = wanted.removeprefix("#").upper()
        exact = next(
            (
                color_id
                for color_id, definition in available.items()
                if str(definition.get("background", "")).removeprefix("#").upper()
                == wanted_key
            ),
            None,
        )
        if exact is not None:
            result[wanted] = str(exact)
            continue
        nearest_id = min(available, key=lambda color_id: color_distance(wanted, available[color_id]))
        result[wanted] = str(nearest_id)
        print(
            f"Color {wanted} is not exact on this calendar; using nearest colorId {nearest_id}."
        )
    return result


def event_payload(
    lesson: Lesson,
    zone_name: str,
    color_id: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    zone = ZoneInfo(zone_name)
    start_minute = parse_hhmm(lesson.start)
    end_minute = parse_hhmm(lesson.end)
    start = first_occurrence(lesson, zone)
    end = start + timedelta(minutes=end_minute - start_minute)
    if end <= start:
        end += timedelta(days=1)
    ends_on = date.fromisoformat(lesson.ends_on)
    until = datetime.combine(ends_on, time.max, tzinfo=zone).astimezone(timezone.utc)
    recurrence = f"RRULE:FREQ=WEEKLY;UNTIL={until.strftime('%Y%m%dT%H%M%SZ')}"
    color_name, color_hex = subject_color(lesson)
    identity = json.dumps(
        {
            "day": lesson.day,
            "start": lesson.start,
            "end": lesson.end,
            "name": lesson.name,
            "location": lesson.location,
            "starts_on": lesson.starts_on,
            "ends_on": lesson.ends_on,
            "timezone": zone_name,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    event_key = hashlib.sha256(identity.encode()).hexdigest()[:40]
    # Calendar event IDs accept base32hex characters only; no hyphen is allowed.
    event_id = f"timetable{event_key[:32]}"
    body: dict[str, Any] = {
        "summary": lesson.name,
        "location": lesson.location,
        "description": "\n".join(
            part
            for part in [
                f"Section: {lesson.section}" if lesson.section else "",
                f"Course dates: {lesson.starts_on} to {lesson.ends_on}",
                f"Weekly on {lesson.day.title()} at {lesson.start}",
                f"Imported once from timetable screenshot (OCR {lesson.ocr_confidence:.1f}%).",
                "Raw OCR:\n" + lesson.raw_ocr if lesson.raw_ocr else "",
            ]
            if part
        ),
        "start": {
            "dateTime": start.isoformat(timespec="seconds"),
            "timeZone": zone_name,
        },
        "end": {
            "dateTime": end.isoformat(timespec="seconds"),
            "timeZone": zone_name,
        },
        "recurrence": [recurrence],
        "reminders": {"useDefault": True},
    }
    if color_id:
        body["colorId"] = color_id
    comparable = {
        key: value
        for key, value in body.items()
        if key not in {"reminders", "extendedProperties"}
    }
    payload_hash = hashlib.sha256(
        json.dumps(comparable, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    body["extendedProperties"] = {
        "private": {
            "source": "timetable-ocr",
            "eventKey": event_key,
            "payloadHash": payload_hash,
            "subjectKey": subject_key(lesson),
            "subjectColor": color_hex,
            "subjectColorName": color_name,
        }
    }
    return event_id, event_key, body


def parse_api_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def verify_event(
    actual: dict[str, Any],
    expected: dict[str, Any],
    event_key: str,
) -> list[str]:
    """Compare a read-back event against what we meant to write."""
    mismatches: list[str] = []
    private = actual.get("extendedProperties", {}).get("private", {})
    if private.get("source") != "timetable-ocr":
        mismatches.append("source marker is missing")
    if private.get("eventKey") != event_key:
        mismatches.append("event key differs")
    if actual.get("summary") != expected["summary"]:
        mismatches.append("name differs")
    if actual.get("location") != expected["location"]:
        mismatches.append("location differs")
    if expected.get("colorId") and actual.get("colorId") != expected["colorId"]:
        mismatches.append("subject color differs")
    try:
        if parse_api_datetime(actual["start"]["dateTime"]) != parse_api_datetime(
            expected["start"]["dateTime"]
        ):
            mismatches.append("start time differs")
        if parse_api_datetime(actual["end"]["dateTime"]) != parse_api_datetime(
            expected["end"]["dateTime"]
        ):
            mismatches.append("end time differs")
    except (KeyError, TypeError, ValueError):
        mismatches.append("start/end time is missing or invalid")
    if expected["recurrence"][0] not in actual.get("recurrence", []):
        mismatches.append("weekly recurrence rule differs")
    return mismatches


def lesson_fields(lesson: Lesson) -> dict[str, Any]:
    """The per-lesson fields shared by the verification report."""
    return {
        "name": lesson.name,
        "day": lesson.day,
        "start": lesson.start,
        "end": lesson.end,
        "location": lesson.location,
    }


def write_event(
    service: Any,
    calendar_id: str,
    event_id: str,
    event_key: str,
    body: dict[str, Any],
) -> str:
    """Create, update, or leave one event alone. Returns the action taken."""
    from googleapiclient.errors import HttpError

    try:
        existing = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as error:
        if error.resp.status != 404:
            raise
        existing = None

    if existing is None:
        service.events().insert(
            calendarId=calendar_id,
            body={**body, "id": event_id},
            sendUpdates="none",
        ).execute()
        return "created"

    private = existing.get("extendedProperties", {}).get("private", {})
    if private.get("eventKey") != event_key:
        raise ImportFailure(
            f"Deterministic event ID collision for {body['summary']!r}; nothing was overwritten."
        )
    if private.get("payloadHash") == body["extendedProperties"]["private"]["payloadHash"]:
        return "unchanged"
    service.events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body=body,
        sendUpdates="none",
    ).execute()
    return "updated"


def sync_lesson(
    service: Any,
    calendar_id: str,
    lesson: Lesson,
    timezone_name: str,
    color_ids: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """Write one lesson and verify the read-back. Returns (result, failures)."""
    _, color_hex = subject_color(lesson)
    event_id, event_key, body = event_payload(
        lesson, timezone_name, color_id=color_ids[color_hex]
    )
    result: dict[str, Any] = {
        **lesson_fields(lesson),
        "subject_color": color_hex,
        "color_id": body.get("colorId", ""),
        "event_id": event_id,
    }

    try:
        action = write_event(service, calendar_id, event_id, event_key, body)
        actual = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except Exception as error:
        result.update(action="failed", verified=False, error=str(error))
        return result, [f"{lesson.name}: {error}"]

    mismatches = verify_event(actual, body, event_key)
    result.update(action=action, verified=not mismatches)
    if mismatches:
        result["mismatches"] = mismatches
        return result, [f"{lesson.name}: verification failed: {', '.join(mismatches)}"]

    result["html_link"] = actual.get("htmlLink", "")
    print(
        f"VERIFIED {action:9} {lesson.day.title():9} {lesson.start}-{lesson.end} "
        f"{lesson.name} @ {lesson.location} [{color_hex}]"
    )
    return result, []


def sync_lessons(
    lessons: list[Lesson],
    calendar_id: str,
    timezone_name: str,
    credentials_path: Path,
    token_path: Path,
    verification_path: Path,
) -> list[dict[str, Any]]:
    service = google_service(credentials_path, token_path)
    color_ids = resolve_event_color_ids(service)

    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for lesson in lessons:
        result, lesson_failures = sync_lesson(
            service, calendar_id, lesson, timezone_name, color_ids
        )
        results.append(result)
        failures.extend(lesson_failures)

    verification = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "calendar_id": calendar_id,
        "timezone": timezone_name,
        "success": not failures,
        "failures": failures,
        "events": results,
    }
    verification_path.parent.mkdir(parents=True, exist_ok=True)
    verification_path.write_text(
        json.dumps(verification, indent=2, ensure_ascii=False) + "\n"
    )
    if failures:
        raise ImportFailure(
            f"{len(failures)} event(s) failed or did not verify. See {verification_path}."
        )
    print(f"\nVerified {len(results)} events in Google Calendar '{calendar_id}'.")
    print(f"Verification report: {verification_path}")
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview a timetable screenshot and optionally import it once into Google Calendar."
    )
    parser.add_argument(
        "image",
        nargs="?",
        type=Path,
        default=DEFAULT_IMAGE,
        help=f"Screenshot path (default: {DEFAULT_IMAGE.name})",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Import after preview and explicit SYNC confirmation; default is preview only",
    )
    parser.add_argument(
        "--from-preview",
        type=Path,
        help="Use an already reviewed JSON preview instead of running OCR again",
    )
    parser.add_argument(
        "--preview-file", type=Path, default=DEFAULT_PREVIEW,
        help=f"Where to write the OCR review file (default: {DEFAULT_PREVIEW.name})",
    )
    parser.add_argument(
        "--verification-file", type=Path, default=DEFAULT_VERIFICATION,
        help=f"Where to write the sync report (default: {DEFAULT_VERIFICATION.name})",
    )
    parser.add_argument(
        "--calendar-id", default=None,
        help="Target calendar ID (default: GCAL_CALENDAR_ID or 'primary')",
    )
    parser.add_argument(
        "--timezone", default=None,
        help="IANA timezone for the events (default: GCAL_TIMEZONE or Asia/Kolkata)",
    )
    parser.add_argument("--date-order", choices=("DMY", "MDY"), default="DMY")
    parser.add_argument("--ocr-lang", default=os.getenv("OCR_LANG", "eng"))
    parser.add_argument("--tesseract-cmd", default=os.getenv("TESSERACT_CMD"))
    parser.add_argument("--credentials", type=Path, default=ROOT / "credentials.json")
    parser.add_argument("--token", type=Path, default=ROOT / "token.json")
    return parser.parse_args()


def load_reviewed(args: argparse.Namespace) -> Review:
    """Re-read a hand-edited preview file and re-validate every lesson."""
    lessons, data = load_preview(args.from_preview)
    errors = [
        f"Lesson {index}: {error}"
        for index, lesson in enumerate(lessons, start=1)
        for error in validate_lesson(lesson)
    ]
    print(f"Loaded {len(lessons)} reviewed lessons from {args.from_preview}.")
    return Review(
        lessons=lessons,
        timezone_name=args.timezone or str(data.get("timezone", "Asia/Kolkata")),
        calendar_id=resolve_calendar_id(args.calendar_id, data.get("calendar_id")),
        warnings=[str(item) for item in data.get("warnings", [])],
        errors=errors,
    )


def build_review(args: argparse.Namespace) -> Review:
    """Run OCR over the screenshot and report whatever was recognized."""
    data = extract_preview(
        image_path=args.image,
        preview_path=args.preview_file,
        language=args.ocr_lang,
        date_order=args.date_order,
        timezone_name=args.timezone or os.getenv("GCAL_TIMEZONE", "Asia/Kolkata"),
        calendar_id=resolve_calendar_id(args.calendar_id, os.getenv("GCAL_CALENDAR_ID")),
        tesseract_cmd=args.tesseract_cmd,
    )
    return Review(
        lessons=[Lesson.from_dict(item) for item in data["lessons"]],
        timezone_name=str(data["timezone"]),
        calendar_id=resolve_calendar_id(data.get("calendar_id")),
        warnings=[str(item) for item in data.get("warnings", [])],
        errors=[str(item) for item in data.get("errors", [])],
    )


def confirm_sync(review: Review) -> bool:
    """Gate the write path behind an interactive, explicit SYNC."""
    if review.errors:
        print(
            "\nSync refused because required fields are missing or invalid.",
            file=sys.stderr,
        )
        return False
    if not sys.stdin.isatty():
        print(
            "\nSync requires an interactive terminal. No data was sent.",
            file=sys.stderr,
        )
        return False
    answer = input(
        f"\nType SYNC to create/update {len(review.lessons)} verified weekly "
        f"events in '{review.calendar_id}': "
    ).strip()
    if answer != "SYNC":
        print("Cancelled. No data was sent.")
    return answer == "SYNC"


def main() -> int:
    args = parse_args()
    try:
        review = load_reviewed(args) if args.from_preview else build_review(args)
        print_preview(
            review.lessons, review.warnings, review.errors,
            args.from_preview or args.preview_file,
        )
        if not args.sync:
            print("\nNo Google Calendar request was made.")
            return 2 if review.errors else 0
        if not confirm_sync(review):
            return 2 if review.errors else 0
        sync_lessons(
            review.lessons,
            review.calendar_id,
            review.timezone_name,
            args.credentials,
            args.token,
            args.verification_file,
        )
        return 0
    except ImportFailure as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
