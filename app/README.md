# Outreach board — local web app

A local web server that serves the ranked role board (feature parity with
`report.html`) and writes outreach records to `data/outreach.db`.

## Run

```bash
uv run python -m app        # serves http://localhost:8765
```

Requires `data/06-final.json` (run stage 6 first). The file is held in memory
and reloaded automatically when its mtime changes, so a pipeline re-run shows
up on refresh without restarting the server.

## What it does

- **Three tabs** (Leadership / IC / Recruiters), full-text search, filter chips
  (Remote-US, Onsite metro, comp band, industry flag, hide-contacted), min-score
  selector, sortable columns, expandable rows with rationale / concerns /
  connections / description excerpt — same behavior as `report.html`.
- **Log outreach**: from any role row, "log outreach" opens a panel pre-filled
  with the company and its `connections[]` as candidate people. Pick one
  person, optionally check additional roles at the same company, set a status,
  add notes. Saves one `outreach` row + N `outreach_role` rows.
  - Company already has an outreach → opens in **edit** mode instead. Never
    creates a second one.
  - Role already owned by another outreach → checkbox disabled with a note.
- **Status changes** (`contacted → active`, anything `→ dead`) are one click
  from the expanded row. No form.
- **Recruiter tab** keeps the contacted toggle — stored as a status-only
  outreach row keyed `recruiter:<profile_url>` so it lands in the same DB.

## Schema (`data/outreach.db`)

Fixed schema. The DB is a local file;
only this app writes it, the pipeline reads it read-only.

```sql
CREATE TABLE outreach (
  id                 INTEGER PRIMARY KEY,
  company_normalized TEXT NOT NULL UNIQUE,
  company_display    TEXT NOT NULL,
  person_profile_url TEXT,
  person_name        TEXT,
  status             TEXT NOT NULL,   -- contacted | applied | active | dead
  contacted_at       TEXT,
  updated_at         TEXT NOT NULL,
  notes              TEXT
);

CREATE TABLE outreach_role (
  outreach_id INTEGER NOT NULL REFERENCES outreach(id) ON DELETE CASCADE,
  role_url    TEXT NOT NULL UNIQUE,
  PRIMARY KEY (outreach_id, role_url)
);
```

Dump it, hand-edit it, diff it: `sqlite3 data/outreach.db .dump`.

## HTTP API

- `GET  /api/bootstrap` — full board + all outreaches (outreach badges merged in)
- `POST /api/outreach` — create; `409` with the existing id if the company is taken
- `PATCH /api/outreach/{id}` — update status / notes / person / contacted_at
- `POST /api/outreach/attach-roles` — `{outreach_id, role_urls[]}`; already-owned roles skipped
- `POST /api/outreach/detach-role` — `{role_url}`
- `DELETE /api/outreach/{id}` — delete an outreach (cascades role attachments)

Deliberately not built: due dates, reminders, "next action"
field, notifications, message drafting, auth, anything remote.
