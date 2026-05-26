"""
AM / PM pack builder.

Splits classified items into two commute-ready briefing packs:
  AM pack  – ALL morning-source items + top manual/priority additions
  PM pack  – evening-source items + remaining manual items

Key policy:
- Morning-source items are always included in the AM pack, regardless of rating.
- Evening-source items are always included in the PM pack, regardless of rating.

Outputs:
  outputs/YYYY-MM-DD_AM_briefing.md   – NotebookLM-ready markdown
  outputs/YYYY-MM-DD_AM_briefing.json – Machine-readable artifact
  (same for PM)
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse
from difflib import SequenceMatcher

from .classifier import classify_all
from .dedupe import normalize_url
from .models import BriefingItem, STREAMS

_OUTPUT_DIR = Path(__file__).parent.parent / "outputs"
_DEFAULT_RAW_CAPTURE_ROOT = Path(r"C:\Users\you\Projects\SecondBrain\raw\news")

_PRIORITY_STREAM = "Ashish's Priority Reads"
_RATING_ORDER = {"Essential": 0, "Important": 1, "Optional": 2, "Unknown": 3}

_AM_TARGET = 15
_PM_TARGET = 10
_SYSTEM_TITLE_PREFIXES = (
    "notebooklm_",
    "briefing_ready",
    "email_sent:",
    "email_error:",
    "secondbrain_ingested:",
    "unique domains:",
    "domain cap check",
    "detailed overview:",
    "c)",
    "d)",
    "e)",
    "f)",
)
_BLOCKED_DOMAINS = {
    "discord.com",
    "www.discord.com",
    "notebooklm.google.com",
}
_TITLE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in", "into",
    "is", "it", "of", "on", "or", "that", "the", "their", "this", "to", "with", "why",
    "what", "when", "new", "your",
}


def _domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _is_publishable_item(item: BriefingItem) -> bool:
    title = (item.title or "").strip().lower()
    if any(title.startswith(prefix) for prefix in _SYSTEM_TITLE_PREFIXES):
        return False

    domain = _domain_of(item.url)
    if domain in _BLOCKED_DOMAINS:
        return False

    summary = (item.summary or "").strip().lower()
    if summary.startswith("notebooklm_") or summary.startswith("briefing_ready"):
        return False

    return True


def _raw_capture_root(config: dict | None) -> Path:
    cfg = config or {}
    return Path(cfg.get("raw_capture_root") or _DEFAULT_RAW_CAPTURE_ROOT)


def _normalized_title_tokens(title: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", (title or "").lower())
    return [t for t in tokens if len(t) > 2 and t not in _TITLE_STOPWORDS]


def _title_signature(title: str) -> str:
    return " ".join(_normalized_title_tokens(title))


def _same_story_recent(current: BriefingItem, prior: BriefingItem) -> bool:
    current_url = normalize_url(current.url or "") if current.url else ""
    prior_url = normalize_url(prior.url or "") if prior.url else ""
    if current_url and prior_url and current_url == prior_url:
        return True

    current_sig = _title_signature(current.title)
    prior_sig = _title_signature(prior.title)
    if not current_sig or not prior_sig:
        return False

    current_tokens = set(current_sig.split())
    prior_tokens = set(prior_sig.split())
    if not current_tokens or not prior_tokens:
        return False

    overlap = len(current_tokens & prior_tokens) / max(1, min(len(current_tokens), len(prior_tokens)))
    ratio = SequenceMatcher(None, current_sig, prior_sig).ratio()
    same_domain = bool(current.url and prior.url and _domain_of(current.url) == _domain_of(prior.url))

    if same_domain and (overlap >= 0.6 or ratio >= 0.72):
        return True
    if overlap >= 0.8 and ratio >= 0.78:
        return True
    return False


def _filter_recent_redundant_items(
    items: List[BriefingItem],
    recent_items: List[BriefingItem],
) -> List[BriefingItem]:
    if not recent_items:
        return items

    filtered: List[BriefingItem] = []
    for item in items:
        if any(_same_story_recent(item, prior) for prior in recent_items):
            continue
        filtered.append(item)
    return filtered


def _load_raw_extract(run_date: str, pack_type: str, item: BriefingItem, config: dict | None) -> str | None:
    raw_dir = _raw_capture_root(config) / run_date / pack_type
    if not raw_dir.exists():
        return None

    matches = sorted(raw_dir.glob(f"{item.item_id}_*.md"))
    if not matches:
        return None

    text = matches[0].read_text(encoding="utf-8")
    text = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
    marker = "## Extracted Content"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    else:
        text = text.strip()
    return text or None


def build_packs(
    items: List[BriefingItem],
    run_date: str,
    config: dict | None = None,
    write_mode: str = "ALL",
    recent_items: List[BriefingItem] | None = None,
) -> Tuple[List[BriefingItem], List[BriefingItem], Path, Path]:
    """
    Classify, sort, split and write AM + PM packs.

    write_mode:
      - ALL: write both AM and PM artifacts
      - AM:  write only AM artifact (PM path returned but not rewritten)
      - PM:  write only PM artifact (AM path returned but not rewritten)

    Returns (am_items, pm_items, am_md_path, pm_md_path).
    """
    cfg = config or {}
    am_target = cfg.get("am_target_items", _AM_TARGET)
    _OUTPUT_DIR.mkdir(exist_ok=True)

    items = classify_all(items, priority_score=cfg.get("priority_score_threshold", 9.0))
    items = [item for item in items if _is_publishable_item(item)]
    lookback_days = int(cfg.get("redundancy_lookback_days", 7) or 0)
    if lookback_days > 0:
        items = _filter_recent_redundant_items(items, recent_items or [])

    def sort_key(item: BriefingItem):
        stream_rank = 0 if item.stream == _PRIORITY_STREAM else 1
        rating_rank = _RATING_ORDER.get(item.rating, 3)
        score = -(item.score_out_of_100 or 0.0)
        return (stream_rank, rating_rank, score)

    sorted_items = sorted(items, key=sort_key)

    morning_items = [item for item in sorted_items if item.source == "morning"]
    evening_items = [item for item in sorted_items if item.source == "evening"]
    manual_items = [item for item in sorted_items if item.source == "manual"]

    am_items: List[BriefingItem] = []
    pm_items: List[BriefingItem] = []

    am_seen: set[str] = set()
    pm_seen: set[str] = set()

    def add_unique(bucket: List[BriefingItem], seen: set[str], item: BriefingItem):
        if item.item_id not in seen:
            bucket.append(item)
            seen.add(item.item_id)

    # Hard rule: include all source-of-truth morning/evening briefing items in their respective packs.
    for item in morning_items:
        add_unique(am_items, am_seen, item)
    for item in evening_items:
        add_unique(pm_items, pm_seen, item)

    # Manual items are used to enrich the relevant pack, but should not displace source-of-truth briefing items.
    for item in manual_items:
        if item.stream == _PRIORITY_STREAM or item.rating == "Essential":
            add_unique(am_items, am_seen, item)
        elif item.rating == "Important" and len(am_items) < am_target:
            add_unique(am_items, am_seen, item)
        else:
            add_unique(pm_items, pm_seen, item)

    for item in am_items:
        item.pack_assigned = "AM"
    for item in pm_items:
        item.pack_assigned = "PM"

    am_md_path = _OUTPUT_DIR / f"{run_date}_AM_briefing.md"
    pm_md_path = _OUTPUT_DIR / f"{run_date}_PM_briefing.md"

    if write_mode in ("ALL", "AM"):
        _write_pack(am_items, "AM", run_date, cfg)
    if write_mode in ("ALL", "PM"):
        _write_pack(pm_items, "PM", run_date, cfg)

    return am_items, pm_items, am_md_path, pm_md_path


def _write_pack(items: List[BriefingItem], pack_type: str, run_date: str, config: dict | None = None) -> Path:
    md_path = _OUTPUT_DIR / f"{run_date}_{pack_type}_briefing.md"
    json_path = _OUTPUT_DIR / f"{run_date}_{pack_type}_briefing.json"

    md_path.write_text(_render_markdown(items, pack_type, run_date, config), encoding="utf-8")
    json_path.write_text(_render_json(items, pack_type, run_date), encoding="utf-8")

    return md_path


def _render_markdown(items: List[BriefingItem], pack_type: str, run_date: str, config: dict | None = None) -> str:
    label = "Morning Commute" if pack_type == "AM" else "Evening Commute"
    target_min = 35

    lines = [
        "---",
        f'title: "AI Briefing {pack_type} Pack – {run_date}"',
        f"pack_type: {pack_type}",
        f"run_date: {run_date}",
        f"item_count: {len(items)}",
        f"target_listening_minutes: {target_min}",
        f"generated_at: {datetime.utcnow().isoformat()}Z",
        "---",
        "",
        f"# AI Briefing: {label} Pack",
        f"**Date:** {run_date}",
        f"**Items:** {len(items)}",
        f"**Target:** ~{target_min} minutes",
        "",
        "## Pack Context",
        "This source pack is optimized for deep technical synthesis.",
        "Each item contains structured operator metadata and, when available, the full raw article extract captured into the knowledge base pipeline.",
        "Treat raw extracts as primary evidence and the metadata as control/context.",
        "",
        "---",
        "",
    ]

    by_stream: dict[str, List[BriefingItem]] = {}
    for item in items:
        by_stream.setdefault(item.stream or "Uncategorized", []).append(item)

    for stream in STREAMS:
        stream_items = by_stream.get(stream, [])
        if not stream_items:
            continue

        lines.append(f"## {stream}")
        lines.append("")

        for item in stream_items:
            lines.append(f"### {item.title}")

            meta_parts = []
            if item.score is not None:
                score_100 = item.score_out_of_100
                score_text = f"{score_100:.1f}/100" if score_100 % 1 else f"{int(score_100)}/100"
                meta_parts.append(f"Score: {score_text}")
            if item.rating and item.rating != "Unknown":
                meta_parts.append(f"Rating: {item.rating}")
            if item.confidence:
                meta_parts.append(f"Confidence: {item.confidence}")
            if meta_parts:
                lines.append("**" + " | ".join(meta_parts) + "**")
                lines.append("")

            if item.is_strong and item.url:
                lines.append(f"**Source:** {item.url}")
                lines.append("")

            if item.summary:
                lines.append(item.summary)
                lines.append("")

            if item.why_bullets:
                lines.append("**Key points:**")
                for bullet in item.why_bullets:
                    lines.append(f"- {bullet}")
                lines.append("")

            if item.leader_move:
                lines.append(f"**Leader move:** {item.leader_move}")
                lines.append("")

            raw_extract = _load_raw_extract(run_date, pack_type, item, config)
            if raw_extract:
                lines.append("**Raw article extract:**")
                lines.append("")
                lines.append(raw_extract)
                lines.append("")

            lines.append("---")
            lines.append("")

    uncategorized = by_stream.get("Uncategorized", [])
    if uncategorized:
        lines.append("## Other")
        lines.append("")
        for item in uncategorized:
            lines.append(f"### {item.title}")
            lines.append(item.summary or "")
            lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def _render_json(items: List[BriefingItem], pack_type: str, run_date: str) -> str:
    return json.dumps(
        {
            "pack_type": pack_type,
            "run_date": run_date,
            "item_count": len(items),
            "generated_at": datetime.utcnow().isoformat(),
            "streams": list({item.stream for item in items if item.stream}),
            "items": [item.to_dict() for item in items],
        },
        indent=2,
    )

