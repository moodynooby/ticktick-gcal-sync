#!/usr/bin/env python3
"""Free 1-way TickTick -> Google Calendar sync.

Uses only official free APIs:
  TickTick OpenAPI  https://api.ticktick.com/open/v1  (Bearer token)
  Google Calendar v3 (OAuth Desktop / GCAL_TOKEN_JSON in CI)

Usage:
  python ticktick_gcal_sync.py --list-projects
  python ticktick_gcal_sync.py --dry-run
  python ticktick_gcal_sync.py
  python ticktick_gcal_sync.py --auth-gcal   # first-time Google auth, writes token.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
STATE_PATH = BASE_DIR / "state.json"
TOKEN_PATH = BASE_DIR / "token.json"
CREDS_PATH = BASE_DIR / "credentials.json"

TICKTICK_BASE = os.getenv("TICKTICK_BASE_URL", "https://api.ticktick.com/open/v1").rstrip("/")
TICKTICK_TOKEN = os.getenv("TICKTICK_ACCESS_TOKEN", "")
GCAL_CALENDAR_ID = os.getenv("GCAL_CALENDAR_ID", "primary")
PROJECT_FILTER = [p.strip() for p in os.getenv("TICKTICK_PROJECT_IDS", "").split(",") if p.strip()]
DELETE_ON_COMPLETE = os.getenv("DELETE_ON_COMPLETE", "true").lower() in ("1", "true", "yes")
SYNC_DAYS_PAST = int(os.getenv("SYNC_DAYS_PAST", "30"))


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
    """GET /project/{id}/data returns project + tasks + columns."""
    data = tick_get(f"/project/{project_id}/data")
    if isinstance(data, dict):
        # shape: {"project": {...}, "tasks": [...], ...}
        for key in ("tasks", "task", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        # fallback: find first list of dicts with title/id
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "title" in v[0]:
                return v
        return []
    return data if isinstance(data, list) else []


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
    except Exception:
        return None


def task_hash(t, start_iso, end_iso, all_day):
    h = hashlib.sha256()
    h.update(json.dumps({
        "title": t.get("title", ""),
        "content": t.get("content", "") or t.get("desc", ""),
        "start": start_iso, "end": end_iso, "allday": all_day,
        "priority": t.get("priority", 0),
        "repeat": t.get("repeatFlag", ""),
        "status": t.get("status", 0),
    }, sort_keys=True).encode())
    return h.hexdigest()[:16]


PRIORITY_COLOR = {0: None, 1: "5", 3: "7", 5: "11"}  # low->yellow, med->teal, high->red


def task_to_event(t, project_name=""):
    """Map TickTick task -> GCal event body. Returns None if no time attr."""
    start_dt = parse_tick_time(t.get("startDate")) or parse_tick_time(t.get("dueDate"))
    due_dt = parse_tick_time(t.get("dueDate")) or start_dt
    if not due_dt:
        return None, None
    is_all_day = bool(t.get("isAllDay", False))
    tz = t.get("timeZone") or "UTC"

    title = t.get("title", "(no title)")
    content = t.get("content", "") or ""
    desc = t.get("desc", "") or ""
    body_text = "\n".join(x for x in [
        content, desc,
        f"TickTick ID: {t.get('id')}",
        f"List: {project_name}" if project_name else "",
        f"Priority: {t.get('priority', 0)}",
    ] if x)

    if is_all_day:
        start_iso = end_iso = due_dt.date().isoformat()
        event = {"summary": title, "description": body_text,
                 "start": {"date": start_iso}, "end": {"date": end_iso}}
        # GCal all-day end is exclusive; add 1 day via date-only +1 handled by caller? keep single-day.
    else:
        # TickTick tasks are points in time; give 60min default duration like native sync
        start = start_dt or due_dt
        end = due_dt
        if end <= start:
            from datetime import timedelta
            end = start + timedelta(minutes=60)
        event = {"summary": title, "description": body_text,
                 "start": {"dateTime": start.isoformat(), "timeZone": tz},
                 "end": {"dateTime": end.isoformat(), "timeZone": tz}}

    h = task_hash(t, json.dumps(event["start"], sort_keys=True),
                  json.dumps(event["end"], sort_keys=True), is_all_day)
    event["extendedProperties"] = {"private": {"ticktickId": t.get("id", ""), "tickHash": h,
                                               "tickProject": t.get("projectId", "")}}
    color = PRIORITY_COLOR.get(t.get("priority", 0))
    if color:
        event["colorId"] = color
    # recurrence: TickTick repeatFlag is RRULE string e.g. "RRULE:FREQ=DAILY;INTERVAL=1"
    repeat = (t.get("repeatFlag") or "").strip()
    if repeat.startswith("RRULE:"):
        event["recurrence"] = [repeat]
    return event, h


# ---- Google ----
def get_gcal_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    creds = None
    token_json = os.getenv("GCAL_TOKEN_JSON", "")
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    elif TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not CREDS_PATH.exists():
            print("ERROR: credentials.json missing. See README step 2.", file=sys.stderr)
            sys.exit(2)
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
        print(f"Wrote {TOKEN_PATH}. For GitHub: gh secret set GCAL_TOKEN_JSON < token.json")
    return build("calendar", "v3", credentials=creds)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def is_completed(t):
    # status 2 = completed per OpenAPI; also completedTime set
    return t.get("status") == 2 or bool(t.get("completedTime"))


def sync(dry_run=False):
    projects = tick_get("/project")
    if PROJECT_FILTER:
        projects = [p for p in projects if p.get("id") in PROJECT_FILTER]
    pname = {p.get("id"): p.get("name", "") for p in projects}

    service = None if dry_run else get_gcal_service()
    state = load_state()
    seen = set()
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
            if is_completed(t):
                if DELETE_ON_COMPLETE and tid in state and not dry_run:
                    try:
                        service.events().delete(calendarId=GCAL_CALENDAR_ID,
                                                eventId=state[tid]["eventId"]).execute()
                        stats["deleted"] += 1
                    except Exception as e:
                        # already gone -> just forget
                        if "404" in str(e) or "Gone" in str(e):
                            stats["deleted"] += 1
                        else:
                            print(f"warn: delete {tid} failed: {e}")
                    del state[tid]
                else:
                    stats["skipped"] += 1
                continue
            event, h = task_to_event(t, pname.get(pid, ""))
            if event is None:
                stats["skipped"] += 1
                continue
            prev = state.get(tid)
            if prev and prev.get("hash") == h:
                stats["skipped"] += 1
                continue
            if dry_run:
                stats["created" if not prev else "updated"] += 1
                print(f"[dry] {'upd' if prev else 'new'}: {event['summary']}")
                continue
            if prev:
                try:
                    service.events().patch(calendarId=GCAL_CALENDAR_ID,
                                           eventId=prev["eventId"], body=event).execute()
                    state[tid] = {"eventId": prev["eventId"], "hash": h}
                    stats["updated"] += 1
                except Exception as e:
                    print(f"warn: update {tid} failed, recreating: {e}")
                    created = service.events().insert(calendarId=GCAL_CALENDAR_ID,
                                                      body=event).execute()
                    state[tid] = {"eventId": created["id"], "hash": h}
                    stats["created"] += 1
            else:
                created = service.events().insert(calendarId=GCAL_CALENDAR_ID,
                                                  body=event).execute()
                state[tid] = {"eventId": created["id"], "hash": h}
                stats["created"] += 1

    # tasks deleted in TickTick (not seen) -> delete GCal mirror
    if not dry_run and DELETE_ON_COMPLETE:
        for tid in [k for k in state if k not in seen]:
            try:
                service.events().delete(calendarId=GCAL_CALENDAR_ID,
                                        eventId=state[tid]["eventId"]).execute()
            except Exception:
                pass
            del state[tid]
            stats["deleted"] += 1

    if not dry_run:
        save_state(state)
    print(json.dumps(stats))
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-projects", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
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
