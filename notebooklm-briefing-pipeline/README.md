# NotebookLM Briefing Pipeline

Converts morning + evening AI briefing files (plus manual Discord URL drops)
into two daily NotebookLM-ready commute packs.

**AM pack** — ~30 min — Priority + Essential + top Important
**PM pack** — ~20 min — Remaining Important + Optional

---

## Quick Start

```bash
cd C:\path\to\workspace\notebooklm-briefing-pipeline

# Run the pipeline for today (auto-discovers briefing files in workspace root)
python run_pipeline.py

# Run for a specific date
python run_pipeline.py --date 2026-03-09

# Check status
python show_status.py
python show_status.py --date 2026-03-09
python show_status.py --runs
```

Core pipeline is stdlib-only, but NotebookLM publishing now uses the installed `jacob-bd/notebooklm-mcp-cli` package (`notebooklm_tools`).

---

## File Structure

```
notebooklm-briefing-pipeline/
├── run_pipeline.py          # Main orchestrator — run this
├── show_status.py           # Status / debug viewer
├── config.json              # Configuration (edit to enable Discord/NotebookLM)
├── state.db                 # SQLite state (created on first run)
├── requirements.txt         # Stdlib only MVP; optional extras documented
│
├── pipeline/
│   ├── models.py            # BriefingItem dataclass
│   ├── db.py                # SQLite helpers
│   ├── ingestion.py         # Briefing file parser
│   ├── dedupe.py            # URL normalization utilities
│   ├── classifier.py        # Stream assignment (keyword heuristic)
│   ├── pack_builder.py      # AM/PM split + markdown/JSON writer
│   ├── notebooklm_adapter.py# Publish adapter (STUB — see below)
│   └── discord_adapter.py   # Discord intake adapter (partial)
│
├── outputs/                 # Generated packs land here
│   └── YYYY-MM-DD_AM_briefing.md
│   └── YYYY-MM-DD_AM_briefing.json
│   └── YYYY-MM-DD_PM_briefing.md
│   └── YYYY-MM-DD_PM_briefing.json
│
└── ARCHITECTURE.md          # Full design doc
```

---

## How It Works (End-to-End)

```
1. File discovery
   run_pipeline.py looks for morning_ai_briefing_YYYY-MM-DD.txt
   and evening_ai_briefing_YYYY-MM-DD.txt in the workspace root.

2. Parsing
   ingestion.py reads the standard briefing format and extracts
   title, URL, score, rating, bullets, summary per story.
   Upstream morning/evening briefings are expected to use the
   canonical `news-article-rating` skill and its structured fields.

3. Deduplication
   Each item gets a content-addressed item_id (SHA-256 of URL or
   title+body). SQLite PRIMARY KEY enforces uniqueness — safe to
   rerun multiple times.

4. Discord intake (optional)
   discord_adapter.py fetches the last 24h of messages from
   channel 1480723611458342923, extracts URLs and notes.
   Disabled by default; activate via config.json.

5. Classification
   classifier.py assigns each item to one of six streams:
     Ashish's Priority Reads / AI Agents / AI Research /
     AI Policy / AI Products / AI Case Studies

6. Pack building
   AM pack: Priority Reads + Essential + Important (≤15 items)
   PM pack: remaining Important + Optional
   Both written as .md (NotebookLM source) and .json (integrations).

7. Publish (STUB)
   notebooklm_adapter.py logs what would be uploaded.
   Replace stub with Google Drive API or Playwright when ready.
```

---

## Enabling Discord Intake

1. Open `config.json`
2. Set `"enabled": true` under `"discord"`
3. Copy `bot_token` from `~/.openclaw/openclaw.json`

```json
"discord": {
  "enabled": true,
  "bot_token": "Bot YOUR_TOKEN_HERE",
  "intake_channel_id": "1480723611458342923"
}
```

The adapter uses Python's `urllib` — no extra packages needed.

---

## Enabling NotebookLM Publishing

NotebookLM publishing is wired to the installed **`jacob-bd/notebooklm-mcp-cli`** package.

Canonical NotebookLM contract skill:
- `C:\path\to\workspace\skills\notebooklm-briefing-orchestration\SKILL.md`
- `C:\path\to\workspace\skills\notebooklm-briefing-orchestration\references\contract.md`
- `C:\path\to\workspace\skills\notebooklm-briefing-orchestration\references\code-map.md`

Use that skill for the NotebookLM communication contract:
- instruction layer vs content layer separation
- source-pack structure
- publish-sequence expectations
- naming and predictability conventions

Keep implementation in code.

Current adapter flow:
1. Load NotebookLM auth profile created by `nlm login`
2. Create a notebook for the generated AM/PM pack
3. Upload the generated markdown file as a source
4. Request an audio overview with a separate focus prompt

### One-time login
Make sure the installed scripts directory is on PATH for the current shell, then log in:

```bash
$env:Path += ';C:\Users\you\AppData\Roaming\Python\Python314\Scripts'
nlm login
```

After login succeeds, rerun the pipeline and it will publish directly.

### Notes
- The adapter currently creates a fresh notebook per published pack.
- The focus prompt is the behavioral steering layer; the uploaded markdown pack is the evidence/content layer.
- If auth is missing, the pipeline fails soft and tells you to run `nlm login`.
- If we later want richer control, we can still extend the adapter with direct MCP/CLI workflows.

---

## Live Cron Integration for T800

Do not copy stale example schedules into production. The live source of truth is OpenClaw cron.

Current operational contract:

- AM NotebookLM publish: `45 5 * * 1-5` America/New_York
  - runs `run_pipeline.py --date {{today}} --mode AM`
  - then runs `scripts/podcast_feed_health_check.py --recent 20 --check 6 --recover --require-date {{today}} --require-mode AM`
- PM NotebookLM publish: `10 16 * * *` America/New_York
  - runs `run_pipeline.py --date {{today}} --mode PM`
  - then runs `scripts/podcast_feed_health_check.py --recent 20 --check 6 --recover --require-date {{today}} --require-mode PM`
- Research publish: `0 12 * * *` America/New_York
  - runs `daily_research_paper_audio.py`
  - then runs the health check with `--require-date {{today}} --require-mode RESEARCH`
- Self-healing feed check: hourly at `:15`
  - runs `scripts/podcast_feed_health_check.py --recent 50 --check 20 --recover`
- Broader feed audit: daily at `9:30 AM`
  - runs `scripts/podcast_feed_health_check.py --recent 100 --check 50 --recover`

Important readiness semantics:

- `NOTEBOOKLM_*_READY` means NotebookLM accepted the publish/audio request; it does **not** by itself mean the Apple podcast feed has the MP3 yet.
- `APPLE_FEED_READY` may only be reported after the health check confirms the required episode is in `https://podcast.example.com/feed.xml` and its public MP3 is reachable.
- If NotebookLM audio is still rendering, report `APPLE_FEED_PENDING` and let the hourly self-healing job retry.

---

## Manual Commands Reference

```bash
# Run pipeline for today (auto-discover briefing files)
python run_pipeline.py

# Run for a past date with a specific file
python run_pipeline.py --date 2026-03-09 --file ../morning_ai_briefing_2026-03-09.txt

# Ingest only (don't build packs yet)
python run_pipeline.py --dry-run

# View summary stats
python show_status.py

# View items for a date
python show_status.py --date 2026-03-09

# View recent pack run history
python show_status.py --runs

# JSON output for scripting
python show_status.py --json
```

---

## Canonical rating skill

Upstream morning and evening briefings should be scored with:
- `C:\path\to\workspace\skills\news-article-rating\SKILL.md`
- `C:\path\to\workspace\skills\news-article-rating\references\rubric.md`

That skill is the source of truth for:
- the 100-point scoring rubric
- Essential / Important / Optional decisions
- override rules
- leader-move and confidence guidance
- 7-day redundancy judgment

Expected per-story fields in briefing files:
- `Title:`
- `Link:`
- `Score: X/100`
- `Rating:`
- `Why:`
- `Leader move:`
- `Confidence:`
- `Summary:`

## Six Learning Streams

| Stream                  | Fires when…                                       |
|-------------------------|---------------------------------------------------|
| Ashish's Priority Reads | rating=Essential OR score ≥ 9.0                   |
| AI Agents               | agentic, multi-agent, tool use, computer use…     |
| AI Research             | arxiv, benchmark, fine-tuning, scaling laws…      |
| AI Policy               | regulation, congress, EU AI Act, governance…      |
| AI Products             | launch, release, API update, chatgpt, claude…     |
| AI Case Studies         | enterprise, deployment, use case, ROI…            |

Default fallback: **AI Products**.

---

## What Is Stubbed

| Feature                   | Status   | To activate                              |
|---------------------------|----------|------------------------------------------|
| Briefing file parser      | ✅ Done   | Works out of the box                    |
| Deduplication             | ✅ Done   | Works out of the box                    |
| Stream classifier         | ✅ Done   | Works out of the box                    |
| AM/PM pack builder        | ✅ Done   | Works out of the box                    |
| SQLite state store        | ✅ Done   | Works out of the box                    |
| Discord intake            | ⚠️ Partial| Add bot_token + enabled=true to config  |
| NotebookLM publish        | ⚠️ Partial | Implemented via `notebooklm-mcp-cli`; requires `nlm login` auth profile |

