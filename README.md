# TickTick -> Google Calendar sync (free, 1-way)

Free 1-way sync using only official free APIs + GitHub Actions (public repo = unlimited minutes).

- TickTick Open API (`https://api.ticktick.com/open/v1`, Bearer token) -> read projects/tasks
- Google Calendar API (OAuth) -> create/update/delete events
- State file `state.json` tracks `ticktick_task_id -> gcal_event_id` for idempotent sync

## Quick start

1. **TickTick token:** TickTick Web -> Avatar -> Settings -> Account -> API Token -> Create. Copy as `TICKTICK_ACCESS_TOKEN`.
   - Limitation (official API): per-project only, no Inbox, no webhooks. Move Inbox items to a list if you want them synced.
2. **Google setup (one time, free):**
   - https://console.cloud.google.com/ -> New project -> Enable `Google Calendar API`
   - OAuth consent screen -> External -> add yourself as test user
   - Credentials -> Create OAuth Client ID -> Desktop app -> download as `credentials.json`
   - Install dependencies with `uv sync`, then run locally once: `uv run ticktick_gcal_sync.py --auth-gcal` (creates `token.json`)
   - You can put local variables such as `TICKTICK_ACCESS_TOKEN` in a `.env` file; the script loads it automatically. Shell environment variables take precedence.
3. **Local test:**
   ```bash
   export TICKTICK_ACCESS_TOKEN="..."
   export GCAL_CALENDAR_ID="primary"
   uv run ticktick_gcal_sync.py --list-projects
   uv run ticktick_gcal_sync.py --dry-run
   uv run ticktick_gcal_sync.py
   ```
4. **GitHub Actions (public repo = free unlimited):**
   - Push this folder to a new **public** repo
   - Repo Settings -> Secrets -> Actions, add:
     - `TICKTICK_ACCESS_TOKEN`
     - `GCAL_TOKEN_JSON` = full contents of `token.json`
     - `GCAL_CALENDAR_ID` (optional, default `primary`)
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
| `SYNC_DAYS_PAST` | `30` | Skip tasks completed/older than this |

Only tasks **with `dueDate` or `startDate`** are synced (matches TickTick native behavior).
