"""
Core data model for the briefing pipeline.

BriefingItem is the canonical unit moving through the system:
  ingest → dedupe → classify → pack → publish
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import List, Optional

STREAMS = [
    "Ashish's Priority Reads",
    "AI Products",
    "AI Research",
    "AI Agents",
    "AI Policy",
    "AI Case Studies",
]

RATINGS = ["Essential", "Important", "Optional", "Unknown"]

SOURCES = ["morning", "evening", "manual"]


@dataclass
class BriefingItem:
    """A single content item flowing through the briefing pipeline."""

    title: str
    summary: str
    source: str           # morning | evening | manual
    source_date: str      # YYYY-MM-DD

    url: Optional[str] = None
    score: Optional[float] = None
    rating: str = "Unknown"
    why_bullets: List[str] = field(default_factory=list)
    leader_move: Optional[str] = None
    confidence: Optional[str] = None

    # Set by classifier
    stream: Optional[str] = None

    # Derived / set by pack builder
    is_strong: bool = False      # True when URL is present
    pack_assigned: Optional[str] = None  # AM | PM | None

    ingested_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )

    def __post_init__(self):
        # Normalize is_strong from URL presence
        self.is_strong = bool(self.url)

    @property
    def score_out_of_100(self) -> Optional[float]:
        """Return the score normalized to a 0-100 scale."""
        if self.score is None:
            return None
        return self.score * 10 if self.score <= 10 else self.score

    @property
    def score_out_of_10(self) -> Optional[float]:
        """Return the score normalized to a 0-10 scale."""
        if self.score is None:
            return None
        return self.score / 10 if self.score > 10 else self.score

    @property
    def item_id(self) -> str:
        """
        Stable 16-char hex ID for deduplication.
        Based on normalized URL if present, else title+summary fingerprint.
        """
        if self.url:
            key = self.url.strip().rstrip("/").lower()
        else:
            key = f"{self.title[:80].lower()}::{self.summary[:100].lower()}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["item_id"] = self.item_id
        d["is_strong"] = bool(self.url)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BriefingItem":
        # Drop computed fields before reconstructing
        d = {k: v for k, v in d.items() if k != "item_id"}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

