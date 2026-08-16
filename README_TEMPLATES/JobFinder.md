# JobFinder

A job-discovery pipeline that ingests live postings, ranks them against an embedded copy of your own resume, and drafts the application material. It never presses send.

Roughly 7,900 lines of async Python across 76 files. Postgres 16 with pgvector, FastAPI and HTMX for the tracker UI, Ollama for local embeddings, Claude Haiku and Sonnet for the parts that need a model.

---

## The shape of it

```
ATS feeds ──► ingest ──► dedup ──► classify (5 stages) ──► rank ──► draft ──► tracker UI
                                                                      │
                                                              status = ready_for_human
                                                                      │
                                                              you click "applied"
```

Six ATS platforms are wired up (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, plus Hacker News "Who's Hiring"). Workday and Eightfold ship as documented stubs, because both need per-tenant setup that cannot be generalised.

---

## What makes it interesting technically

### The classifier is a funnel, and the funnel is a cost model

Five stages, ordered cheapest first, each one shrinking the set the next one has to look at:

| Stage | What runs | Marginal cost |
|---|---|---|
| 1 | Keyword taxonomies (`classify/keywords.py`) | free |
| 2 | Regex pass, whole-token word-boundary matching, superlinear multi-match with diminishing returns (`classify/regex_pass.py`) | free |
| 3 | Local embeddings, `nomic-embed-text-v1.5` at 768 dims via Ollama | free, local |
| 4 | Claude Haiku triage, batched 10 postings per call | paid |
| 5 | `final_rank()` composite scorer | free |

Stage 4 is gated: a posting is skipped entirely if its cosine similarity to the profile vector is below 0.35 **and** it scored fewer than 2 fit-keyword hits. Both signals come back in the same `SELECT` so the gate costs no extra round trip. The expensive model only ever sees postings the free stages could not confidently reject.

### One choke point for every billable token

`llm/client.py` exposes exactly one function, `call_messages()`, and every LLM call in the codebase goes through it. Every call writes a row to `llm_cost_log` with model, operation label, input/output tokens, cache-read and cache-write tokens, and computed USD.

Pricing lives in one table (`llm/pricing.py`) rather than inline at call sites, with cache writes billed at 1.25x input and cache reads at 0.10x. Spend is queryable after the fact, grouped by operation, which is the only way to find out that a prompt you changed last week quietly got 4x more expensive.

### Cache-stable retrieval

The drafter builds a system prefix from the top 40 portfolio chunks by cosine distance to the aggregate profile vector, marked `cache_control: ephemeral`. That prefix only re-bills at the cache-read rate if the text is **byte-identical** call to call, and cosine ordering is not stable when scores tie.

So `retrieval.top_for_system()` retrieves by similarity, then re-sorts the retrieved set by `id` before concatenating. Retrieval order picks the chunks; a deterministic key orders them. The per-posting user message gets its own separate top-8 retrieval against that posting's embedding, which is allowed to vary because it is outside the cached prefix.

### The ranker is opinionated on purpose

```python
raw = (fit_score
       * (1 - 0.6 * check_risk_score)
       * freshness            # exp decay, 72h half-life
       * salary_factor        # rewards disclosure, penalises sub-floor
       * remote_flex          # remote 1.1, hybrid 1.05, onsite 1.0, unspecified 0.95
       * location_match       # 1.0 in target geography, 0.3 outside
       * qc_bonus)            # 1.08 for QC-headquartered companies
```

Multiplicative rather than a weighted sum, so any single hard disqualifier collapses the score instead of being averaged away by six good signals. `check_risk_score` is a discount, not a filter, so a high-risk posting with an excellent fit still surfaces, just lower.

### The invariant that does not move

Every draft lands as `applications.status = 'ready_for_human'`. The only code path in the repository that sets `applied_at` is a button in the tracker UI. There is no auto-submit flag, no config toggle, no CLI escape hatch. A bot that applies to jobs on your behalf is a bot that damages your name at machine speed, so the pipeline stops one step short by construction.

### Other things worth a look

- **Dedup on two levels.** A partial unique index on `title_company_hash` catches exact re-posts; pgvector cosine catches the same job re-listed with a rewritten title.
- **19 routes, zero SPA.** The tracker is FastAPI templates plus HTMX. Kanban board, posting detail, run console, metrics dashboard, `/healthz`, and PDF resume and cover-letter generation, all server-rendered.
- **9 tables, 8 Alembic migrations,** including a `v_daily_metrics` materialised view that the `/metrics` page reads.
- **Structured logging** via structlog, Sentry, and a fire-and-forget `metric_events` write from every long-running step.

---

## Stack

Python 3.12, asyncio throughout. psycopg3 with a connection pool, SQLAlchemy plus Alembic for migrations, Postgres 16 with pgvector, Pydantic v2 for settings and models, Typer for the CLI, FastAPI with Jinja2 and HTMX for the UI, xhtml2pdf for document rendering, httpx with tenacity for ATS clients, Ollama for embeddings, the Anthropic API for triage and drafting, Resend for the daily digest, structlog and Sentry for observability. Linted with ruff, type-checked with pyright, 10 pytest suites.

---

## Running it

**Prerequisites:** Python 3.12+, [uv](https://github.com/astral-sh/uv), Docker Desktop. Ollama and an Anthropic API key are needed from stage 3 onward.

```bash
cp .env.example .env          # DATABASE_URL is the only required value to start
uv sync --extra dev
docker compose up -d postgres # pgvector/pgvector:pg16
uv run alembic upgrade head
uv run jfb seed load          # ~45 seeded target companies in four tiers
```

On Linux and macOS, `make bootstrap` does the last four steps.

Then:

```bash
uv run jfb ingest all                    # fetch from every configured source
uv run jfb profile ingest                # chunk + embed your resume and prefs
uv run jfb classify all                  # regex → embed → haiku → rank
uv run jfb top --limit 30
uv run jfb draft top --limit 15          # Sonnet + RAG, writes ready_for_human
uv run jfb web serve --port 8000         # http://localhost:8000
```

`systemd/` has five example service and timer units if you want it running unattended: hourly ingest, two-hourly classify, nightly batch draft at 02:10, a 07:00 digest email, and a monthly portfolio recrawl.

Your own `profile/resume.md` and `profile/prefs.md` are what the whole thing ranks against. Copy the `.example` files and rewrite them to be about you.

---

## Layout

```
src/job_finder/
  ats/          one async client per ATS platform
  ingest/       orchestrator + non-ATS sources + repository
  classify/     keywords, regex pass, embeddings, Haiku triage, rank
  drafter/      RAG retrieval, prompts, fit gate, Sonnet draft
  llm/          the single Anthropic client + pricing + cost logging
  ui/           FastAPI + HTMX tracker (19 routes)
  notify/       daily digest via Resend
  portfolio/    crawl, extract, chunk, embed your own sites
  resume/       tailored PDF rendering + presets
  scrape/       Playwright harness + LinkedIn/Indeed skeletons (opt-in)
  ranking.py    final_rank() composite scorer
migrations/     8 Alembic revisions
docs/           spec, deep research, local run guide
tests/          10 pytest suites
```

---

## Honest state

- **Working end to end** for the ATS path. Ingest, dedup, all five classifier stages, ranking, drafting, PDF generation, the tracker UI, the digest, and the metrics view all run.
- **The LinkedIn and Indeed scrapers are skeletons.** URL builders, CAPTCHA detection, and a stealth-lite Chromium harness are pinned and tested, but `_parse_results_page()` returns `[]` until you verify current DOM selectors yourself. Everything downstream works unchanged if you do.
- **Workday and Eightfold clients are stubs** with per-tenant implementation notes in the file.
- **It is a personal tool.** The seed company list, the keyword weights, the salary band, and the Quebec bonus all encode one person's search. Fork it and rewrite `classify/keywords.py` and `profile/` before it is useful to you.

---

## Screenshots

TODO. Capture these four, save them to `docs/img/`, and link them here:

1. **`kanban.png`** — run `jfb web serve`, open `/`, and capture the full kanban board at 1440px wide with at least a dozen real postings spread across columns. This is the money shot; make sure the column headers and counts are legible.
2. **`posting-detail.png`** — open any posting with a completed draft (`/posting/{id}`) and capture the detail view showing the fit score, the rationale text Haiku produced, and the `ready_for_human` status badge.
3. **`metrics.png`** — `/metrics`, after at least a week of runs so the daily-metrics view has a real series rather than one bar.
4. **`cost-log.png`** — a terminal capture of `SELECT operation, count(*), round(sum(usd_cost)::numeric, 4) FROM llm_cost_log GROUP BY operation ORDER BY 3 DESC;`. Proof that the cost accounting is real and not a claim.

A 20 second screen recording of the triage-to-draft loop would carry this repo better than any of the stills. If you record one, put it at the top.
