# Connections Finder

Turn your LinkedIn connections export into a ranked list of open roles **at companies
where you know someone**, plus a ranked list of recruiters worth contacting directly.

It reads your connections, works out which companies they work at, finds each company's
public job board (Greenhouse, Ashby, Lever, SmartRecruiters, Recruitee, Workday), pulls
every open posting, filters by title and location, scores each role against a profile you
write, and ranks the result by role fit × how strong your connection there is. The output
is a single self-contained `report.html` and an optional local web app for tracking who
you have reached out to.

It is a single-user tool that runs on your own machine. The title filter keeps only
postings whose role matches one of your **target-role tracks**, configured in
`settings.yaml`; without a `tracks:` section you get the built-in software-engineering
tracks (Engineering leadership and Senior/Staff software engineering), which use a fast
regex path. Custom tracks send each distinct title to the LLM once (cached) instead. See
[Customizing](#customizing).

Pairs well with [job-matcher](https://github.com/thefotes/job-matcher), an MCP server that
lets your AI agent evaluate and track postings; it can import `data/02-boards.jsonl`.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- An OpenAI-compatible LLM endpoint that supports strict `json_schema` output: OpenAI,
  or a local server (Ollama, LM Studio, vLLM, LiteLLM, ...)
- Optional: Node.js, used by the audit's headless smoke test of the report

## Setup

```bash
git clone https://github.com/thefotes/connections-finder.git
cd connections-finder
uv sync

cp .env.example .env                     # LLM endpoint, key and model
cp settings.example.yaml settings.yaml   # your employers, exclusions, location
cp profile.example.md profile.md         # the rubric roles are scored against
```

### 1. Export your LinkedIn data

LinkedIn → **Settings & Privacy → Data privacy → Get a copy of your data**. Choose the
archive with at least **Connections** and **Messages** (the "basic" archive is enough).
When the email arrives, unzip it and copy the two CSVs into `input/`:

```
input/Connections.csv
input/messages.csv     # optional; enables DM-volume and recency scoring
```

### 2. Fill in your settings

- **`.env`**: `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `LLM_MODEL`. For a local model, point
  `OPENAI_BASE_URL` at it (e.g. `http://localhost:11434/v1` for Ollama).
- **`settings.yaml`**: your employment history (connections made while you worked
  somewhere are probably former colleagues and get a boost), companies to exclude, and
  your home state and onsite metro for the location filter.
- **`profile.md`**: who you are and what you want, in plain language. The LLM scores
  every eligible role against it.

Check the LLM connection:

```bash
uv run python -c "from pipeline.common import llm_healthcheck; print(llm_healthcheck())"
```

### 3. Run it

```bash
uv run python run_all.py      # stages 0-6, then the audit
open report.html
```

The first run takes a while: it probes job boards for every company in your network and
makes one LLM call per eligible role. Every HTTP response and LLM verdict is cached in
`data/cache/`, so re-runs are fast and resume where they stopped.

`run_all.py 3` runs from stage 3 onward; `run_all.py 3 5` runs stages 3 to 5 only.

### 4. Optional: track outreach in the web app

```bash
uv run python -m app          # http://localhost:8765
```

Log who you contacted at each company. Roles at companies you have already contacted are
pushed down the next time stage 6 runs, so the board keeps steering you toward new
companies. See [`app/README.md`](app/README.md).

## Pipeline

Each stage reads only the artifact before it and writes its own to `data/`.

| Stage | Command | Output |
|---|---|---|
| 0 parse | `uv run python -m pipeline.stage0_parse` | `00-connections.jsonl` (connections joined with DM stats) |
| 1 score | `uv run python -m pipeline.stage1_score` | `01-connections-scored.jsonl`, `01-recruiters.jsonl` |
| 2 boards | `uv run python -m pipeline.stage2_boards` | `02-boards.jsonl` (verified job boards), `02-unresolved.csv` |
| 3 fetch | `uv run python -m pipeline.stage3_fetch` | `03-jobs-raw.jsonl` |
| 4 filter | `uv run python -m pipeline.stage4_filter` | `04-jobs-eligible.jsonl`, `rejects-title.jsonl`, `rejects-location.jsonl` |
| 5 score | `uv run python -m pipeline.stage5_score_roles` | `05-jobs-scored.jsonl` |
| 6 assemble | `uv run python -m pipeline.stage6_assemble` | `06-final.json`, `report.html` |
| audit | `uv run python -m pipeline.audit` | `audit-report.json` (invariant checks) |

Every filter logs what it dropped, with a reason, to a `data/rejects-*.jsonl` file. Read
them to tune the filters.

## Customizing

- **Re-score without re-crawling**: edit `profile.md` or the weights in `config.py`, then
  `uv run python -m pipeline.stage5_score_roles && uv run python -m pipeline.stage6_assemble`.
- **Weights and knobs**: `config.py` (connection score weights, role score weights,
  location multipliers, crawl concurrency, staffing agencies to skip, optional title
  de-boost patterns).
- **Boards auto-discovery missed**: copy `manual-boards.example.yaml` to
  `manual-boards.yaml` and paste careers URLs, then re-run from stage 2.
- **Target roles (tracks)**: configure `tracks` in `settings.yaml` — a list of named
  tracks, each with example `titles` (and optional `exclude` words and
  `manages_people`). Omitting it gives the built-in software-engineering tracks, which
  use a regex fast path; custom tracks send each distinct title to the LLM once (cached),
  so re-runs stay free. The stage 4 filter keeps only postings whose role matches one of
  your tracks.

## Privacy

Your connections' names, profile URLs, emails and messages never leave your machine.

- **HTTP** goes only to public ATS job-board APIs; the audit checks this.
- **The LLM** sees job postings, company names, your connections' job titles (no names)
  and your `profile.md`. Use a local model if you would rather nothing leave your network.
- Everything personal (`input/`, `settings.yaml`, `profile.md`, `data/`, `report.html`,
  `.env`) is gitignored.

## Tests

```bash
uv run pytest -q
```

## License

[MIT](LICENSE)
