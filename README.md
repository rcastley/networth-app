# Net Worth Tracker

Self-hosted personal net-worth tracker. FastAPI + HTMX + Tailwind + SQLite, packaged for Docker Compose.

## What it does

Track balances across accounts at any cadence — weekly, daily, multiple times a day. Each snapshot stores per-account balances at a point in time, with both date and time so intra-day captures are unambiguous.

The dashboard shows the latest figures with period-over-period deltas, a trend chart (weekly / monthly / quarterly), an allocation doughnut, and a hierarchical category breakdown (category → parent group → leaf account) that expands inline. Accounts can be organised into groups — e.g. all the sub-pots of a single bank account under one parent — so the dashboard and the snapshot entry form mirror how you actually think about your money.

CSV import / export is built in for backfilling history and round-tripping to spreadsheets. An in-app help panel (the "?" button in the top-right) walks first-time users through the workflow; the content lives in `app/help.yaml` and can be edited without touching code.

No authentication — designed to sit on a LAN or behind your existing reverse-proxy auth (Authelia, Tailscale, etc.).

## Quick start

```bash
cd networth-app
docker compose up -d --build
docker compose logs -f networth
```

Then open <http://localhost:8000>. The "?" button in the top-right opens an in-app guide that explains the workflow in a few minutes.

First-run behaviour: SQLite DB is created at `./data/networth.db`, Alembic migrations run, and the schema is seeded with 7 starter categories (Cash, Investments, Restricted, Pension, Property, Liability, Life Insurance). No accounts or balances — head to the Accounts page to add your own, then "+ New snapshot" to enter your first set.

## Configuration

Set via `docker-compose.yml` or a `.env` file alongside it:

- `PORT` — host port to expose (default `8000`)
- `TZ` — timezone for log timestamps (default `Europe/London`)
- `DATABASE_URL` — SQLAlchemy URL; defaults to `sqlite:////data/networth.db`

## Local development (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install
npm run build:css          # produces app/static/app.css
DATABASE_URL=sqlite:///$(pwd)/dev.db uvicorn app.main:app --reload
```

`npm run watch:css` rebuilds the CSS file on template edits.

In-app help is context-aware — each page (Dashboard, Snapshots, Accounts, etc.) gets its own set of sections, plus a shared "General" block that appears on every view. Content lives in `app/help.yaml`:

```yaml
views:
  dashboard:
    title: Dashboard           # rendered as "Help · Dashboard" in the panel header
    sections:
      - id: dash-headlines
        title: Headline cards
        body: |
          <p>HTML body — write whatever's easiest to author.</p>

global:
  - id: glb-concepts
    title: Key concepts
    body: |
      <p>This section is appended to every view.</p>
```

Reorder, add, or delete sections freely; restart the app to pick up changes. To add help for a new view, give that route a `view_id="<name>"` in `app/main.py` and add a `views.<name>` entry to the YAML.

Note: if you reference Tailwind utility classes inside YAML bodies (e.g. `class="list-disc pl-5"`), they get picked up because `tailwind.config.js` includes `app/help.yaml` in its `content` paths. Rebuild CSS (`npm run build:css` locally, or rebuild the Docker image) after adding utility classes.

## Data and backups

The SQLite file lives at `./data/networth.db` on the host (bind-mounted into the container). Back it up however you back up the rest of the host. While the app is running, prefer:

```bash
sqlite3 ./data/networth.db ".backup '/path/to/backup.db'"
```

over a raw file copy to guarantee a consistent snapshot.

To wipe everything and start fresh: stop the container, delete `./data/networth.db`, restart. Seed runs again.

## Migrations

The app uses Alembic. On startup it auto-runs `alembic upgrade head`. Existing installs that pre-date Alembic (had tables but no `alembic_version` row) are auto-stamped to the initial revision before upgrading — no manual intervention needed.

Current migrations:

- `0001` — initial schema (categories, accounts, snapshots, balances)
- `0002` — adds `parent_id` and `is_group` to accounts (account grouping)
- `0003` — `snapshot_date` becomes `DateTime` and drops the unique constraint (multiple snapshots per day allowed)

To create a new migration after a model change:

```bash
docker compose exec networth alembic revision --autogenerate -m "describe change"
# edit alembic/versions/NEW_FILE.py to verify the autogen
docker compose restart networth
```

## Routes

- `/` — Dashboard (headlines, deltas, trend, allocation, expandable category breakdown)
- `/snapshots` — list, edit, delete
- `/snapshots/new` — new snapshot (date defaults to right-now, balances pre-filled from previous snapshot)
- `/accounts` — inline edit, set parent group, mark as group, add new
- `/categories` — inline edit + add
- `/import` — CSV import / export
- `/export` — direct CSV download
- `/api/chart-data?period=weekly|monthly|quarterly` — JSON for the trend chart
- `/healthz` — container health check

Errors are content-negotiated: browsers get a styled HTML error page with Back / Dashboard buttons; clients sending `Accept: application/json` (or hitting any `/api/*` endpoint) get JSON.

## Stack

- **FastAPI** — web framework
- **SQLAlchemy 2.0** + **Alembic** — ORM and migrations
- **SQLite** — single-file database (sized for hundreds of years of weekly snapshots)
- **Jinja2** — server-rendered templates
- **HTMX** — light interactivity without an SPA
- **Tailwind CSS** (built at image-build time, no runtime CDN) — styling
- **Chart.js** — line + doughnut charts
- **PyYAML** — loads in-app help content from `app/help.yaml`

Total Python deps: 7. Runs comfortably in <100 MB of RAM.
