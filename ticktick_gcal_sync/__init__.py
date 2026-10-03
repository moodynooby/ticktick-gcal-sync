#!/usr/bin/env python3
"""Free 1-way TickTick -> Google Calendar sync.

Uses only official free APIs:
  TickTick OpenAPI  https://api.ticktick.com/open/v1  (Bearer token)
  Google Calendar v3 (OAuth Desktop / GCAL_TOKEN_JSON in CI)

Usage:
  uv run ticktick-gcal-sync --list-projects
  uv run ticktick-gcal-sync --dry-run
  uv run ticktick-gcal-sync
  uv run ticktick-gcal-sync --auth-gcal   # first-time Google auth, writes token.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# state.json, token.json, and credentials.json live at the repo root (this
# package's parent) in development; leave them next to the checkout.
BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "state.json"
TOKEN_PATH = BASE_DIR / "token.json"
CREDS_PATH = BASE_DIR / "credentials.json"

TICKTICK_DEFAULT_BASE = "https://api.ticktick.com/open/v1"


def env_str(name, default=""):
    """Read an env var, treating unset and blank alike.

    os.getenv's default only applies when the var is unset. In GitHub Actions an
    optional secret such as `${{ secrets.GCAL_CALENDAR_ID }}` expands to "", which
    previously produced requests to /calendars//events (HTTP 404).
    """
    return (os.getenv(name) or "").strip() or default


def env_int(name, default):
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


TICKTICK_BASE = env_str("TICKTICK_BASE_URL", TICKTICK_DEFAULT_BASE).rstrip("/")
TICKTICK_TOKEN = env_str("TICKTICK_ACCESS_TOKEN")
GCAL_CALENDAR_ID = env_str("GCAL_CALENDAR_ID", "primary")
PROJECT_FILTER = [item.strip() for item in env_str("TICKTICK_PROJECT_IDS").split(",") if item.strip()]
DELETE_ON_COMPLETE = env_str("DELETE_ON_COMPLETE", "true").lower() in ("1", "true", "yes")
SYNC_DAYS_PAST = env_int("SYNC_DAYS_PAST", 30)
PRIORITY_COLOR = {0: None, 1: "5", 3: "7", 5: "11"}  # low->yellow, med->teal, high->red


# ---- TickTick ----
def tick_headers():
    if not TICKTICK_TOKEN:
        print("ERROR: set TICKTICK_ACCESS_TOKEN env var.", file=sys.stderr)
        sys.exit(2)
    return {"Authorization": f"Bearer {TICKTICK_TOKEN}", "Content-Type": "application/json"}


def tick_get(path):
    r = requests.get(f"{TICKTICK_BASE}{path}", headers=tick_headers(), timeout=30)
    if r.status_code == 401:
        print("TickTick 401: bad/expired token. Recreate API Token.", file=sys.stderr)
        sys.exit(2)
    r.raise_for_status()
    return r.json()


def list_projects():
    projects = tick_get("/project")
    for p in projects:
        print(f"{p.get('id')}  {p.get('name')}")
    return projects


def get_project_tasks(project_id):
    """GET /project/{id}/data returns {"project": {...}, "tasks": [...], ...}."""
    data = tick_get(f"/project/{project_id}/data")
    if isinstance(data, list):
        return data
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return tasks if isinstance(tasks, list) else []


def parse_tick_time(s):
    """TickTick format: "yyyy-MM-dd'T'HH:mm:ssZ" e.g. 2019-11-13T03:00:00+0000"""
    if not s:
        return None
    try:
        # normalize +0000 -> +00:00
        if len(s) >= 5 and (s[-5] in "+-") and s[-2:].isdigit():
            s = s[:-2] + ":" + s[-2:]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # untrusted external payload: any bad shape means "no time"
        return None


def is_completed(t):
    # status 2 = completed per OpenAPI; also completedTime set
    return t.get("status") == 2 or bool(t.get("completedTime"))


def is_too_old(t, today, days_past=SYNC_DAYS_PAST):
    """True when a task's due date fell out of the SYNC_DAYS_PAST window.

    Dates are compared in UTC, which is what TickTick sends when a task carries
    no timezone of its own.
    """
    due = parse_tick_time(t.get("dueDate"))
    return due is not None and due.date() < today - timedelta(days=days_past)


def task_description(t, project_name):
    return "\n".join(part for part in [
        t.get("content", "") or "",
        t.get("desc", "") or "",
        f"TickTick ID: {t.get('id')}",
        f"List: {project_name}" if project_name else "",
        f"Priority: {t.get('priority', 0)}",
    ] if part)


def task_hash(t, event):
    """Content digest, so an unchanged task is never re-sent to Google.

    start/end are hashed as pre-serialized strings: that is the shape already
    stored in state.json, so existing entries keep matching after a refactor.
    """
    return hashlib.sha256(json.dumps({
        "title": t.get("title", ""),
        "content": t.get("content", "") or t.get("desc", ""),
        "start": json.dumps(event["start"], sort_keys=True),
        "end": json.dumps(event["end"], sort_keys=True),
        "allday": "date" in event["start"],
        "priority": t.get("priority", 0),
        "repeat": t.get("repeatFlag", ""),
        "status": t.get("status", 0),
    }, sort_keys=True).encode()).hexdigest()[:16]


def task_to_event(t, project_name=""):
    """Map TickTick task -> GCal event body. Returns (event, hash), or (None, None)."""
    start_dt = parse_tick_time(t.get("startDate")) or parse_tick_time(t.get("dueDate"))
    due_dt = parse_tick_time(t.get("dueDate")) or start_dt
    if not due_dt:
        return None, None
    summary = t.get("title", "(no title)")
    description = task_description(t, project_name)

    if t.get("isAllDay", False):
        # Google Calendar treats an all-day `end.date` as exclusive, so a
        # single-day task needs end = start + 1 day to be visible at all.
        day = due_dt.date()
        event = {"summary": summary, "description": description,
                 "start": {"date": day.isoformat()},
                 "end": {"date": (day + timedelta(days=1)).isoformat()}}
    else:
        # TickTick tasks are points in time; give a 60min default duration
        # like the native sync does.
        start = start_dt or due_dt
        end = due_dt if due_dt > start else start + timedelta(hours=1)
        tz = t.get("timeZone") or "UTC"
        event = {"summary": summary, "description": description,
                 "start": {"dateTime": start.isoformat(), "timeZone": tz},
                 "end": {"dateTime": end.isoformat(), "timeZone": tz}}

    digest = task_hash(t, event)
    event["extendedProperties"] = {"private": {
        "ticktickId": t.get("id", ""),
        "tickHash": digest,
        "tickProject": t.get("projectId", ""),
    }}
    color = PRIORITY_COLOR.get(t.get("priority", 0))
    if color:
        event["colorId"] = color
    # recurrence: TickTick repeatFlag is RRULE string e.g. "RRULE:FREQ=DAILY;INTERVAL=1"
    repeat = (t.get("repeatFlag") or "").strip()
    if repeat.startswith("RRULE:"):
        event["recurrence"] = [repeat]
    return event, digest


# ---- Google ----
def get_gcal_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/calendar"]
    token_json = env_str("GCAL_TOKEN_JSON")
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), scopes)
    elif TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), scopes)
    else:
        creds = None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not CREDS_PATH.exists():
            print("ERROR: credentials.json missing. See README step 2.", file=sys.stderr)
            sys.exit(2)
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), scopes)
        creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
        print(f"Wrote {TOKEN_PATH}. For GitHub: gh secret set GCAL_TOKEN_JSON < token.json")
    return build("calendar", "v3", credentials=creds)


def create_event(service, event):
    return service.events().insert(calendarId=GCAL_CALENDAR_ID, body=event).execute()["id"]


def delete_event(service, event_id):
    """Delete a mirror event. False means it failed for a reason other than it
    already being gone (404/410)."""
    from googleapiclient.errors import HttpError

    try:
        service.events().delete(calendarId=GCAL_CALENDAR_ID, eventId=event_id).execute()
    except HttpError as error:
        return error.resp.status in (404, 410)
    except Exception:
        return False
    return True


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def sync_task(service, state, t, project_name, dry_run, today):
    """Mirror one TickTick task. Returns the stats action for it."""
    tid = t.get("id", "")

    if is_completed(t):
        if not (DELETE_ON_COMPLETE and tid in state and not dry_run):
            return "skipped"
        deleted = delete_event(service, state[tid]["eventId"])
        del state[tid]
        if not deleted:
            print(f"warn: delete {tid} failed")
            return "skipped"
        return "deleted"

    if is_too_old(t, today):
        return "skipped"

    event, digest = task_to_event(t, project_name)
    if event is None:
        return "skipped"

    prev = state.get(tid)
    if prev and prev.get("hash") == digest:
        return "skipped"
    if dry_run:
        print(f"[dry] {'upd' if prev else 'new'}: {event['summary']}")
        return "updated" if prev else "created"

    if not prev:
        state[tid] = {"eventId": create_event(service, event), "hash": digest}
        return "created"
    try:
        service.events().patch(calendarId=GCAL_CALENDAR_ID,
                               eventId=prev["eventId"], body=event).execute()
    except Exception as e:
        print(f"warn: update {tid} failed, recreating: {e}")
        state[tid] = {"eventId": create_event(service, event), "hash": digest}
        return "created"
    state[tid] = {"eventId": prev["eventId"], "hash": digest}
    return "updated"


def sync(dry_run=False):
    projects = tick_get("/project")
    if PROJECT_FILTER:
        projects = [p for p in projects if p.get("id") in PROJECT_FILTER]
    pname = {p.get("id"): p.get("name", "") for p in projects}

    service = None if dry_run else get_gcal_service()
    state = load_state()
    seen = set()
    today = datetime.now(timezone.utc).date()
    stats = {"created": 0, "updated": 0, "deleted": 0, "skipped": 0}

    for p in projects:
        pid = p.get("id")
        try:
            tasks = get_project_tasks(pid)
        except requests.HTTPError as e:
            print(f"warn: project {pname.get(pid)} ({pid}) fetch failed: {e}")
            continue
        for t in tasks:
            tid = t.get("id", "")
            if not tid:
                continue
            seen.add(tid)
            stats[sync_task(service, state, t, pname.get(pid, ""), dry_run, today)] += 1

    # tasks deleted in TickTick (not seen) -> delete GCal mirror
    if not dry_run and DELETE_ON_COMPLETE:
        for tid in [k for k in state if k not in seen]:
            delete_event(service, state[tid]["eventId"])  # already gone still counts
            del state[tid]
            stats["deleted"] += 1

    if not dry_run:
        save_state(state)
    print(json.dumps(stats))
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list-projects", action="store_true", help="print project IDs and names")
    ap.add_argument("--dry-run", action="store_true", help="report intended changes, write nothing")
    ap.add_argument("--auth-gcal", action="store_true",
                    help="run Google OAuth flow and exit")
    args = ap.parse_args()
    if args.list_projects:
        list_projects()
    elif args.auth_gcal:
        get_gcal_service()
        print("Google auth OK.")
    else:
        sync(dry_run=args.dry_run)


if __name__ == "__main__":
    main()