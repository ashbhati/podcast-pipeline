"""
Briefing file ingestion.

Parses the morning/evening AI briefing plain-text format:

  B) Today's Stories (ranked)

  1) Story Title
  Link: https://...
  Score: 94/100
  Rating: Essential
  Why:
  - Bullet one
  - Bullet two
  Leader move: ...
  Confidence: High
  Summary: Full paragraph...

  2) Next story ...

  C) Cross-Story Signals
  ...

Also handles markdown-formatted variants using **Field:** syntax.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .models import BriefingItem

# Matches a lettered section header:  "B) Today's Stories"  or  "## B. STORIES"
_SECTION_LETTER_RE = re.compile(r"^([A-Z])[.)]\s", re.IGNORECASE)

# Story-block start:  "1) Title"  or  "### 1. Title"  or  "Title: ..."
_STORY_START_RE = re.compile(
    r"^\d+[.)]\s+\S|^###\s+\d+[.)]\s*|^(?:\*\*)?Title:(?:\*\*)?\s*\S",
    re.IGNORECASE,
)


def parse_briefing_file(
    path: Path,
    source: str = "morning",
    source_date: Optional[str] = None,
) -> List[BriefingItem]:
    """Parse a briefing text file. Returns a list of BriefingItems."""
    text = path.read_text(encoding="utf-8", errors="replace")

    if source_date is None:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
        source_date = m.group(1) if m else datetime.today().strftime("%Y-%m-%d")

    if source not in ("morning", "evening", "manual"):
        source = "evening" if "evening" in path.name.lower() else "morning"

    return _extract_stories(text, source, source_date)


def parse_manual_url(
    url: str,
    title: Optional[str] = None,
    note: Optional[str] = None,
    source_date: Optional[str] = None,
) -> BriefingItem:
    """Create a BriefingItem from a manually submitted URL (e.g. from Discord)."""
    if source_date is None:
        source_date = datetime.today().strftime("%Y-%m-%d")
    return BriefingItem(
        title=title or url,
        summary=note or "",
        source="manual",
        source_date=source_date,
        url=url,
        rating="Unknown",
        is_strong=True,
    )


# ─── Internal parsing ────────────────────────────────────────────────────────

def _extract_stories(text: str, source: str, source_date: str) -> List[BriefingItem]:
    """
    Find the B) stories section then split into story blocks.
    Falls back to scanning the whole file if no section header is found.
    """
    lines = text.split("\n")
    stories_lines: List[str] = []
    in_stories = False

    for line in lines:
        stripped = line.strip()

        # Detect canonical story sections like:
        # - "B) Today's Stories (ranked)"
        # - "B) Top 5 Stories (ranked)"
        # - "## B. STORIES"
        if not in_stories:
            if re.match(r"^[Bb][.)]\s*.*stor", stripped, re.IGNORECASE) or re.match(
                r"^##\s*[Bb]\.?\s*.*stor", stripped, re.IGNORECASE
            ):
                in_stories = True
            continue

        # Stop when we hit the next lettered section (C, D, E…)
        letter_m = _SECTION_LETTER_RE.match(stripped)
        if letter_m and letter_m.group(1).upper() not in ("B",):
            break

        stories_lines.append(line)

    if not stories_lines:
        # No B) section found — try parsing the whole file
        stories_lines = lines

    return _split_and_parse("\n".join(stories_lines), source, source_date)


def _split_and_parse(stories_text: str, source: str, source_date: str) -> List[BriefingItem]:
    """Split stories block into individual story blocks and parse each."""
    lines = stories_text.split("\n")
    blocks: List[List[str]] = []
    current: List[str] = []

    for line in lines:
        if _STORY_START_RE.match(line.strip()) and current:
            blocks.append(current)
            current = [line]
        elif _STORY_START_RE.match(line.strip()):
            current = [line]
        else:
            current.append(line)

    if current:
        blocks.append(current)

    items = []
    for block_lines in blocks:
        block = "\n".join(block_lines).strip()
        if block:
            item = _parse_story_block(block, source, source_date)
            if item:
                items.append(item)
    return items


def _parse_story_block(block: str, source: str, source_date: str) -> Optional[BriefingItem]:
    """Parse one story block text into a BriefingItem."""
    lines = block.split("\n")

    # Title is the first non-empty line; strip numbering/markdown/field prefixes.
    raw_title = lines[0].strip()
    title = re.sub(r"^\d+[.)]\s+", "", raw_title)
    title = re.sub(r"^###\s+(?:\d+[.)]\s+)?", "", title).strip()
    title = re.sub(r"^(?:\*\*)?Title:(?:\*\*)?\s*", "", title, flags=re.IGNORECASE).strip()
    if (not title or len(title) < 3) and re.match(r"^(?:\*\*)?Title:(?:\*\*)?\s*$", raw_title, re.IGNORECASE):
        for candidate in lines[1:]:
            candidate = candidate.strip()
            if candidate:
                title = candidate
                break
    if not title or len(title) < 3:
        return None

    rest = "\n".join(lines[1:])

    def _get(field: str, default=None):
        """Extract a field value, handling both  'Field: val'  and  '**Field:** val'."""
        pattern = rf"(?:\*\*)?{re.escape(field)}:(?:\*\*)?\s*(.+?)(?=\n(?:\*\*)?(?:Link|Score|Rating|Why|Leader move|Confidence|Summary):|\Z)"
        m = re.search(pattern, rest, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else default

    # URL
    url = _get("Link")
    if url:
        # Take only the first token (URLs have no spaces)
        url = url.split()[0]
    if url and url.lower() in ("none", "n/a", "-", "\u2013", ""):
        url = None

    # Score: support both legacy "9.4/10" and canonical "94/100"
    score_raw = _get("Score")
    score: Optional[float] = None
    if score_raw:
        m2 = re.search(r"([0-9.]+)\s*/\s*(10|100)", score_raw)
        if m2:
            value = float(m2.group(1))
            scale = int(m2.group(2))
            score = value if scale == 100 else value
        else:
            m2 = re.search(r"([0-9.]+)", score_raw)
            if m2:
                score = float(m2.group(1))

    # Rating
    rating_raw = _get("Rating") or "Unknown"
    rating = rating_raw.split()[0]  # "Essential" from "Essential\n..."

    # Why bullets
    why_pattern = r"(?:\*\*)?Why:(?:\*\*)?\s*(.*?)(?=\n(?:\*\*)?(?:Leader move|Confidence|Summary):|\Z)"
    why_m = re.search(why_pattern, rest, re.IGNORECASE | re.DOTALL)
    why_raw = why_m.group(1).strip() if why_m else ""
    why_bullets = [
        ln.strip().lstrip("-").lstrip("*").strip()
        for ln in why_raw.split("\n")
        if ln.strip().lstrip("-").lstrip("*").strip()
    ]

    # Leader move
    leader_move = _get("Leader move")
    if leader_move:
        # Trim to first line if multi-line (extra lines may be next field bleed)
        leader_move = leader_move.split("\n")[0].strip()

    # Confidence (single word or short phrase)
    confidence_raw = _get("Confidence")
    confidence = confidence_raw.split("\n")[0].strip() if confidence_raw else None

    # Summary — greedy to end of block
    sum_m = re.search(
        r"(?:\*\*)?Summary:(?:\*\*)?\s*(.+)",
        rest,
        re.IGNORECASE | re.DOTALL,
    )
    summary = sum_m.group(1).strip() if sum_m else None

    if not summary:
        summary = " ".join(why_bullets[:2]) if why_bullets else title

    return BriefingItem(
        title=title,
        summary=summary,
        source=source,
        source_date=source_date,
        url=url,
        score=score,
        rating=rating,
        why_bullets=why_bullets,
        leader_move=leader_move,
        confidence=confidence,
        is_strong=bool(url),
    )

