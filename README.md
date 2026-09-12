# Job Application Monitor

A private, single-user Django application for tracking job applications on one computer. It combines manual tracking with read-only Gmail detection, AI-assisted extraction, a review inbox for uncertain changes, durable job-posting snapshots, follow-ups, interviews, documents, contacts, and outcome reports.

The service binds to loopback by default and stores its SQLite database, uploaded files, usage ledger, and backups under `data/`. Gmail OAuth tokens and the optional DeepSeek key are kept outside the database in the operating-system credential store.

## Main capabilities

- Today dashboard for overdue work, upcoming tasks/interviews, recent changes, missing postings, and sync health.
- Searchable, sortable application table plus a stage board with customizable card details and complete application history.
- Gmail backfill and incremental history sync with safe checkpoints, retries, per-account locking, and an uncertainty review inbox.
- Immutable posting snapshots from recognized public URLs, pasted text, or the Chrome/Brave capture extension.
- Follow-up drafts and reminders, interview rounds and `.ics` export, contacts, and version-labelled documents.
- Reports with explicit denominators, response-time samples, source comparisons, and resume-version comparisons.
- Consistent SQLite-and-media backups, restore verification, CSV export, and local security checks.

## Architecture

```text
Browser UI / Chrome or Brave extension
              │
              ▼
       Django views and forms
              │
      domain services / review
       │          │          │
       ▼          ▼          ▼
    SQLite    Gmail API   DeepSeek API
       │       read-only    relevant text only
       ▼
 managed media + backups
```

The UI is server-rendered HTML/CSS with a small amount of JavaScript and HTMX. A user-level systemd timer invokes the same idempotent Gmail sync command every five minutes; no Redis, queue server, or hosted backend is required.

On the Board, use **Customize cards** to choose which details appear on every card. Priority and application date are shown by default; company, role, and the stage-move control always remain visible. The choice is saved for the whole local app.

## Quick start

Requirements: Linux, Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and a modern browser.

```bash
uv sync --dev
cp .env.example .env
uv run python manage.py migrate
uv run python manage.py create_local_user
./scripts/run.sh
```

Open <http://127.0.0.1:8765/> and sign in with the local account you created. Before relying on the app, replace `DJANGO_SECRET_KEY` in `.env` and restrict the data directory:

```bash
chmod 700 data
uv run python manage.py security_check
```

The launcher makes a rotating backup before migrations when an existing database is present.

## Gmail and AI setup

1. Create a Google Cloud OAuth Desktop application with the read-only Gmail scope.
2. Download its client JSON to `credentials.json`, or point `GOOGLE_OAUTH_CLIENT_SECRETS` at it.
3. Connect an account, preview the scope in Settings, then run the initial import:

```bash
uv run python manage.py gmail_connect
uv run python manage.py gmail_sync --all --days 90
```

For AI extraction, either set `DEEPSEEK_API_KEY` in `.env` or store it in the OS keyring:

```bash
uv run python -m tracker.integrations.secrets set-deepseek-key
```

Relevant candidate email/posting text is sent to the configured AI endpoint. Extraction stops when the recorded monthly ceiling is reached; manual tracking remains available. Google OAuth applications in Testing may issue short-lived refresh tokens, so reconnect when Settings reports that action is required.

## Background operation

Review paths in the unit files if the checkout is not at `~/vs-code-projects/job_application_monitoring`, then install the web, sync, and backup units:

```bash
mkdir -p ~/.config/systemd/user
cp scripts/job-monitor.service scripts/job-monitor-sync.* scripts/job-monitor-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now job-monitor.service job-monitor-sync.timer job-monitor-backup.timer
```

The sync timer runs after one minute and every five minutes thereafter. The backup timer runs daily. Both catch up after a suspended/off computer when systemd can do so. To keep user services running while logged out, optionally run `loginctl enable-linger "$USER"`.

## Browser capture extension (Chrome or Brave)

Brave is Chromium-based and supports this same Manifest V3 extension; there is no separate Brave build to keep in sync. Open `brave://extensions` (or `chrome://extensions` in Chrome), enable **Developer mode**, choose **Load unpacked**, and select `extension/`. In the application, open Settings → Browser capture extension, generate a pairing token, and paste it into the popup. The extension uses temporary `activeTab` access and can write only to the local capture endpoint. See [Brave's extension guidance](https://support.brave.com/hc/en-us/articles/360017909112-How-can-I-add-extensions-to-Brave) for browser-specific installation notes.

## Common operations

```bash
# Test and static checks
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run

# Backup, restore, and export
uv run python manage.py backup --keep 7
uv run python manage.py restore data/backups/jobmon-YYYYMMDDTHHMMSSZ
uv run python manage.py export_csv --out applications.csv

# Retry stored AI failures or inspect duplicate candidates
uv run python manage.py gmail_retry_failed
uv run python manage.py merge_duplicate_applications
```

Stop the web and sync services before restore. The duplicate command is a dry run unless `--apply` is supplied; inspect every candidate because the same company and title can represent separate applications.

## Repository map

- `config/` — Django settings, root URLs, WSGI/ASGI entry points.
- `tracker/` — models, views, forms, matching, sync, capture, reports, backups, and commands.
- `templates/`, `static/` — server-rendered interface and local assets.
- `extension/` — Manifest V3 Chrome/Brave (Chromium) posting-capture extension.
- `scripts/` — launcher and user-level systemd units/timers.
- `fixtures/`, `tests/` — anonymized extraction/capture fixtures and behavior tests.
- `data/` — ignored runtime database, media, usage records, and backups.
- `IMPLEMENTATION_PLAN.md` — original product and reliability requirements.
- `docs/PROJECT_GUIDE.md` — complete architecture, flow, operation, and maintenance guide.

See [the project guide](docs/PROJECT_GUIDE.md) for the full repository walkthrough, environment-variable reference, data model, flows, deployment notes, design decisions, and known limitations.
