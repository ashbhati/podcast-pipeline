# NotebookLM Briefing Pipeline — Architecture

## Purpose

Convert the existing morning + evening AI briefing cron outputs (plus
manually-shared URLs in Discord) into two daily NotebookLM-ready packs
optimised for commute listening:

| Pack | Target      | Content                                     |
|------|-------------|---------------------------------------------|
| AM   | ~30 minutes | Priority Reads + Essential + top Important  |
| PM   | ~20 minutes | Remaining Important + Optional              |

---

## System Context

```text
+====================================================================================+
|                         PODCAST PIPELINE WORKFLOW                                   |
+====================================================================================+

  1) SOURCE INTAKE                 2) CURATION                    3) NOTEBOOKLM
  ----------------                 -----------                    -------------

  +----------------------+         +----------------------+        +----------------------+
  | Written AI briefings |         | Parse + normalize    |        | Create fresh         |
  | AM / PM text files   |-------> | stories, URLs,       |------> | NotebookLM notebook  |
  +----------------------+         | scores, metadata     |        +----------+-----------+
                                   +----------+-----------+                   |
  +----------------------+                    |                              v
  | Manual article links |                    v                   +----------------------+
  | Discord intake       |-------> +----------------------+        | Upload markdown      |
  +----------------------+         | Deduplicate          |        | source pack          |
                                   | SQLite item IDs      |        +----------+-----------+
  +----------------------+         +----------+-----------+                   |
  | Daily research paper |                    |                              v
  | optional deep dive   |----------------+   v                   +----------------------+
  +----------------------+                | +----------------------+        | Request Audio        |
                                          | | Classify streams     |        | Overview with focus  |
                                          | | Priority / Agents /  |        | prompt               |
                                          | | Research / Policy /  |        +----------+-----------+
                                          | | Products / Cases     |                   |
                                          | +----------+-----------+                   v
                                          |            |                    +----------------------+
                                          |            v                    | Record publish run   |
                                          +-> +----------------------+      | in state.db          |
                                             | Build source packs    |      +----------+-----------+
                                             | AM / PM / Research    |                 |
                                             | Markdown + JSON       |                 |
                                             +----------------------+                 |
                                                                                    v
+====================================================================================+
|                         4) PODCAST PUBLICATION                                      |
+====================================================================================+

                         +----------------------+
                         | Feed health check    |
                         | required episode?    |
                         +----------+-----------+
                                    |
                                    v
                             +-------------+
                             | Audio ready?|
                             +------+------+
                                    |
                +-------------------+-------------------+
                |                                       |
                | no                                    | yes
                v                                       v
  +------------------------------+       +------------------------------+
  | APPLE_FEED_PENDING           |       | Download/cache audio          |
  | Hourly self-healing retries  |       +--------------+---------------+
  +---------------+--------------+                      |
                  |                                     v
                  |                      +------------------------------+
                  |                      | Transcode WAV to MP3          |
                  |                      +--------------+---------------+
                  |                                     |
                  |                                     v
                  |                      +------------------------------+
                  |                      | Upload MP3 to Cloudflare R2   |
                  |                      | audio/<notebook_id>.mp3       |
                  |                      +--------------+---------------+
                  |                                     |
                  |                                     v
                  |                      +------------------------------+
                  |                      | Generate RSS feed.xml         |
                  |                      +--------------+---------------+
                  |                                     |
                  |                                     v
                  |                      +------------------------------+
                  |                      | Cloudflare Worker serves      |
                  |                      | feed + byte-range audio       |
                  |                      +--------------+---------------+
                  |                                     |
                  |                                     v
                  |                      +------------------------------+
                  |                      | Apple Podcasts + podcast apps |
                  |                      +------------------------------+
                  |
                  +-------------> next health check cycle

Readiness rule: NotebookLM publish success means audio was requested; Apple/podcast
readiness only starts after the feed contains the episode and the public MP3 is reachable.
```

---

## Data Model

### BriefingItem (pipeline/models.py)

| Field          | Type           | Notes                                   |
|----------------|----------------|-----------------------------------------|
| `item_id`      | str (16 hex)   | SHA-256 of normalized URL or title+body |
| `title`        | str            | Story headline                          |
| `summary`      | str            | Body text / why summary                 |
| `source`       | str            | morning \| evening \| manual            |
| `source_date`  | str            | YYYY-MM-DD                              |
| `url`          | str?           | Source URL (None for weak items)        |
| `score`        | float?         | Story importance score (0–10)           |
| `rating`       | str            | Essential / Important / Optional        |
| `why_bullets`  | list[str]      | 3-bullet rationale from briefing        |
| `leader_move`  | str?           | Action/implication for a leader         |
| `confidence`   | str?           | High / Medium / Low                     |
| `stream`       | str?           | Set by classifier                       |
| `is_strong`    | bool           | True when URL is present                |
| `pack_assigned`| str?           | AM \| PM after pack build               |

### SQLite Tables (state.db)

**briefing_items** — one row per unique item (item_id PK = dedup key)
**pack_runs** — audit log of every AM/PM build

---

## Pipeline Stages

### 1. Ingestion (pipeline/ingestion.py)

Parses the standard briefing format produced by existing cron jobs.

Those upstream cron jobs should use the canonical rating skill:
- `C:\path\to\workspace\skills\news-article-rating\SKILL.md`
- `C:\path\to\workspace\skills\news-article-rating\references\rubric.md`

Expected story schema:

```
Title: Story Title
Link: https://...
Score: 94/100
Rating: Essential
Why:
- Bullet one
- Bullet two
- Bullet three
Leader move: Pilot
Confidence: High
Summary: ...
```

`parse_briefing_file(path)` returns `List[BriefingItem]`.

### 2. Deduplication (pipeline/db.py + pipeline/dedupe.py)

- Primary key = `item_id` = SHA-256 of normalized URL (or title+body hash)
- `insert_item()` silently drops duplicates
- `dedupe.normalize_url()` strips UTM params / trailing slashes before hashing
- Running the pipeline twice for the same date is safe

### 3. Classification (pipeline/classifier.py)

Keyword-matching against concatenated title + summary + why_bullets.

| Stream                   | Priority | Key signals                              |
|--------------------------|----------|------------------------------------------|
| Ashish's Priority Reads  | Highest  | rating==Essential OR score≥9.0           |
| AI Agents                | 2        | agentic, multi-agent, tool use, devin…   |
| AI Research              | 3        | arxiv, benchmark, fine-tun, scaling…     |
| AI Policy                | 4        | regulation, congress, EU AI Act…         |
| AI Products              | 5        | launch, release, chatgpt, anthropic…     |
| AI Case Studies          | 6        | enterprise, deployment, use case…        |

Fallback: "AI Products" if no keyword matches.

### 4. Pack Building (pipeline/pack_builder.py)

Sort order: Priority stream → Essential → Important (by score desc).

```
AM pack:  Priority Reads + Essential + Important (up to am_target_items)
PM pack:  remaining Important + Optional
```

Overflow from AM spills into PM head.

**Content policy applied in markdown render:**
- Strong item (has URL): URL + summary + bullets
- Weak item (no URL):    summary + bullets only

### Canonical NotebookLM contract

The NotebookLM communication contract is documented in:
- `C:\path\to\workspace\skills\notebooklm-briefing-orchestration\SKILL.md`
- `C:\path\to\workspace\skills\notebooklm-briefing-orchestration\references\contract.md`
- `C:\path\to\workspace\skills\notebooklm-briefing-orchestration\references\code-map.md`

That skill is the source of truth for:
- instruction layer vs content layer separation
- source-pack schema expectations
- publish protocol expectations
- what belongs in code vs in the contract

### 5. Output Artifacts

Two files per pack type:

| File                              | Use                              |
|-----------------------------------|----------------------------------|
| `YYYY-MM-DD_AM_briefing.md`       | Upload to NotebookLM as source   |
| `YYYY-MM-DD_AM_briefing.json`     | Machine-readable for integrations |

The markdown has YAML frontmatter (title, pack_type, item_count, etc.)
followed by stream-grouped content.

### 6. NotebookLM Adapter (pipeline/notebooklm_adapter.py)

Backed by the installed **`jacob-bd/notebooklm-mcp-cli`** package via its
Python library (`notebooklm_tools`). Interface:
```python
adapter.publish_pack(pack_path: Path, notebook_name: str) -> str | None
```

Current implementation:
1. Load NotebookLM auth profile created by `nlm login`
2. Create notebook
3. Upload generated markdown pack as a source
4. Request audio overview generation with a separate focus prompt

Architectural rule:
- focus prompt = synthesis behavior / hidden steering
- uploaded pack = content, evidence, metadata, raw extracts

Current limitation:
- Requires a valid local `nlm` auth profile (`default` by default)
- Audio URL normalization is still thin; adapter returns notebook URL

---

## Configuration (config.json)

```json
{
  "discord": {
    "enabled": false,          // set true + bot_token to activate intake
    "bot_token": "",           // copy from openclaw.json
    "intake_channel_id": "1480723611458342923"
  },
  "notebooklm": {
    "enabled": true,
    "profile_name": "default",
    "create_audio": true,
    "audio_format": "deep_dive",
    "audio_length": "long"
  },
  "pipeline": {
    "am_target_items": 15,
    "pm_target_items": 10,
    "priority_score_threshold": 9.0
  }
}
```

---

## Live Cron Integration

The live OpenClaw cron configuration is the source of truth; this file is documentation only.

Current operational contract:

- AM publish: `45 5 * * 1-5` America/New_York, then required feed verification for today's AM episode.
- Research publish: `0 12 * * *` America/New_York, then required feed verification for today's research episode.
- PM publish: `30 15 * * *` America/New_York, then required feed verification for today's PM episode.
- Self-healing feed check: hourly at `:15`, using `--recent 50 --check 20 --recover`.
- Broader feed audit: daily at `9:30 AM`, using `--recent 100 --check 50 --recover`.

Readiness semantics:

- NotebookLM publish success only means the notebook exists and audio was requested.
- Apple/podcast readiness requires `scripts/podcast_feed_health_check.py` to confirm the required episode appears in `https://podcast.example.com/feed.xml` and its public MP3 is reachable.
- Delayed NotebookLM audio must be reported as `APPLE_FEED_PENDING`, not `APPLE_FEED_READY`.

---

## What Is Stubbed vs. Fully Implemented

| Component              | Status        | Notes                                        |
|------------------------|---------------|----------------------------------------------|
| File parser            | ✅ Full        | Parses real briefing format                  |
| Deduplication          | ✅ Full        | SQLite PK, URL normalization                 |
| Stream classifier      | ✅ Full        | Keyword heuristic, 6 streams                 |
| AM/PM pack builder     | ✅ Full        | Split logic + markdown + JSON output         |
| SQLite state store     | ✅ Full        | briefing_items + pack_runs tables            |
| Discord intake HTTP    | ⚠️ Partial     | Real HTTP code written; disabled until token |
| NotebookLM publish     | ⚠️ Partial     | Uses `notebooklm-mcp-cli`; requires `nlm login` auth |

