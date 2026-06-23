# Productive Time Sync

A personal desktop tool that automatically fills out your [Productive.io](https://productive.io) timesheets. It fetches your project bookings, accounts for holidays and absences, and creates time entries for you — either for the full month or incrementally, week by week.

## How It Works

1. **Pick a month** — defaults to the current month, filling only through today
2. **Add absences** — sick days, vacation, PTO, etc. (fetched live from Productive)
3. **Preview** — see exactly what will be created before committing
4. **Execute** — creates absence bookings and time entries via the Productive API

Re-running is always safe — existing entries are detected and skipped automatically.

## Quick Start

```bash
# 1. Clone and enter the project
cd productive-time-sync

# 2. Set up Python environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env
# Edit .env with your Productive API token, org ID, and person ID

# 4. Launch
./start.sh
```

## Finding Your Credentials

```bash
# Look up your person ID:
python sync_time_entries.py --whoami

# List holiday calendars (to verify your country code):
python sync_time_entries.py --list-calendars

# List available absence event types:
python sync_time_entries.py --list-events
```

## Environment Variables

| Variable | Description |
|---|---|
| `PRODUCTIVE_API_TOKEN` | Your Productive API token |
| `PRODUCTIVE_ORG_ID` | Your organization ID |
| `PRODUCTIVE_PERSON_ID` | Your person ID (use `--whoami` to find it) |
| `PRODUCTIVE_SUBSIDIARY_NAME` | Your office/subsidiary (e.g. `PDUS`) |
| `PRODUCTIVE_COUNTRY_CODE` | Country for holiday calendar (e.g. `US`) |

## Running

### Desktop App (recommended)

```bash
./start.sh
```

Opens a native macOS window with the wizard UI. Closes cleanly when done.

### Browser Mode (for development)

```bash
source .venv/bin/activate
uvicorn api:app --reload --port 8181
```

Then open http://localhost:8181 in your browser.

### CLI (original script, still works)

```bash
python sync_time_entries.py --dry-run          # preview current/previous month
python sync_time_entries.py --month 2025-06    # specific month
python sync_time_entries.py --no-prompts       # skip absence prompts
```

## Building a Standalone .app

```bash
source .venv/bin/activate
pip install py2app
python setup.py py2app
```

The app bundle will be in `dist/Productive Time Sync.app` — drag it to your Applications folder.

## Project Structure

```
productive-time-sync/
├── sync_time_entries.py   # Core logic + CLI interface
├── api.py                 # FastAPI REST endpoints
├── app.py                 # PyWebView launcher (native window)
├── static/
│   └── index.html         # Wizard UI (vanilla HTML/JS/CSS)
├── start.sh               # Quick launcher script
├── setup.py               # py2app config for .app bundle
├── requirements.txt       # Python dependencies
├── .env.example           # Credential template
└── .env                   # Your credentials (git-ignored)
```
