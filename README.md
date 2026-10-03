# Calendar utilities

## One-time timetable screenshot import

`schedule_ocr_gcal_sync.py` performs a **manual, one-time** import from a timetable screenshot. It:

- detects variable row heights, extra internal rules, wrapped text, and multiple courses in one time cell;
- previews every event with its **name, weekday, start/end time, date range, section, and location**;
- refuses to sync if any required event field is missing;
- creates timezone-aware weekly Google Calendar events with an inclusive end date;
- assigns the same color to the same normalized subject and resolves the writable color ID from Google Calendar;
- re-reads every event after writing it and writes a verification report;
- uses deterministic event IDs, so accidentally running it again updates/verifies rather than duplicates its own events.

The default command is preview-only and makes **no Google Calendar request**.

### Reproducible setup

Python dependencies and versions are locked in `uv.lock`; `uv` manages the environment. Tesseract is a native OCR engine, so it remains a system package.

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install tesseract-ocr tesseract-ocr-eng

# macOS alternative
brew install tesseract

uv sync
```

On Windows, install Tesseract with `winget install UB-Mannheim.TesseractOCR`; if it is not on `PATH`, set `TESSERACT_CMD` to the executable path.

Configure the OAuth desktop client as described in **Google setup** below, or reuse the existing `credentials.json` and `token.json`. These files and `.env` are ignored by Git.

### 1. Preview only

```bash
uv run python schedule_ocr_gcal_sync.py
```

The default screenshot is `Screenshot from 2026-09-24 12.22.02@2x.png`. For another image:

```bash
uv run python schedule_ocr_gcal_sync.py path/to/timetable.png
```

Review both the terminal preview and `.schedule_ocr_preview.json`. The current screenshot has a visibly cropped 46-pixel Saturday column; the preview reports that it was skipped. Replace the image if Saturday data matters.

If OCR makes a correctable error, edit the corresponding `name`, `code`, `section`, `starts_on`, `ends_on`, or `location` in `.schedule_ocr_preview.json`. Re-preview that exact file without OCR:

```bash
uv run python schedule_ocr_gcal_sync.py --from-preview .schedule_ocr_preview.json
```

### 2. Sync only the reviewed preview

```bash
uv run python schedule_ocr_gcal_sync.py --from-preview .schedule_ocr_preview.json --sync
```

The script prints the reviewed events again and requires the exact typed word `SYNC`. It then creates/updates events on the selected calendar and verifies each event by reading it back. The report is saved to `.schedule_ocr_verification.json`.

Optional `.env` settings:

```dotenv
GCAL_CALENDAR_ID=primary
GCAL_TIMEZONE=Asia/Kolkata
OCR_LANG=eng
# TESSERACT_CMD=/full/path/to/tesseract
```

This workflow is intentionally not scheduled and does not run continuously.

## TickTick -> Google Calendar sync (free, 1-way)

Free 1-way sync using only official free APIs + GitHub Actions (public repo = unlimited minutes).

- TickTick Open API (`https://api.ticktick.com/open/v1`, Bearer token) -> read projects/tasks
- Google Calendar API (OAuth) -> create/update/delete events
- State file `state.json` tracks `ticktick_task_id -> gcal_event_id` for idempotent sync

### Quick start

1. **TickTick token:** TickTick Web -> Avatar -> Settings -> Account -> API Token -> Create. Copy as `TICKTICK_ACCESS_TOKEN`.
   - Limitation (official API): per-project only, no Inbox, no webhooks. Move Inbox items to a list if you want them synced.
2. **Google setup (one time, free):**
   - https://console.cloud.google.com/ -> New project -> Enable `Google Calendar API`
   - OAuth consent screen -> External -> add yourself as test user
   - Credentials -> Create OAuth Client ID -> Desktop app -> download as `credentials.json`
   - Install dependencies with `uv sync`, then run locally once: `uv run ticktick-gcal-sync --auth-gcal` (creates `token.json`)
    - Create a local `.env` file for development:
       ```dotenv
       TICKTICK_ACCESS_TOKEN=your_ticktick_token
       GCAL_CALENDAR_ID=primary
       ```
    - The script loads `.env` automatically. Shell environment variables take precedence.
3. **Local test:**
   ```bash
   export TICKTICK_ACCESS_TOKEN="..."
   export GCAL_CALENDAR_ID="primary"
   uv run ticktick-gcal-sync --list-projects
   uv run ticktick-gcal-sync --dry-run
   uv run ticktick-gcal-sync
   ```
4. **GitHub Actions (public repo = free unlimited):**
   - Push this folder to a new **public** repo
    - Add these repository secrets under **Settings -> Secrets and variables -> Actions**:
     - `TICKTICK_ACCESS_TOKEN`
     - `GCAL_TOKEN_JSON` = full contents of `token.json`
     - `GCAL_CALENDAR_ID` (optional, default `primary`)
       - `TICKTICK_PROJECT_IDS` (optional, comma-separated project IDs)
    - With GitHub CLI, run these commands from the project directory:
       ```bash
       gh secret set TICKTICK_ACCESS_TOKEN --body "$(sed -n 's/^TICKTICK_ACCESS_TOKEN=//p' .env)"
       gh secret set GCAL_TOKEN_JSON < token.json
       gh secret set GCAL_CALENDAR_ID --body "primary"
       # Optional:
       gh secret set TICKTICK_PROJECT_IDS --body "project-id-1,project-id-2"
       ```
    - Do not commit `.env`, `credentials.json`, or `token.json`; they are already ignored by `.gitignore`.
   - Workflow `.github/workflows/sync.yml` runs every 30 min (~1440 min/mo, safe even for private). Manual run via Actions tab.
   - `state.json` is committed back automatically (needs `contents: write`).

## Config (env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `TICKTICK_ACCESS_TOKEN` | (required) | Bearer token |
| `TICKTICK_BASE_URL` | `https://api.ticktick.com/open/v1` | Use `https://api.dida365.com/open/v1` if your account is Dida365 |
| `TICKTICK_PROJECT_IDS` | empty = all | Comma-separated list to limit sync, e.g. `id1,id2` |
| `GCAL_CALENDAR_ID` | `primary` | Target calendar ID |
| `GCAL_TOKEN_JSON` | - | For CI: contents of token.json |
| `DELETE_ON_COMPLETE` | `true` | Delete GCal event when TickTick task completed/deleted |
| `SYNC_DAYS_PAST` | `30` | Don't mirror tasks whose due date is older than this many days |

Only tasks **with `dueDate` or `startDate`** are synced (matches TickTick native behavior).
