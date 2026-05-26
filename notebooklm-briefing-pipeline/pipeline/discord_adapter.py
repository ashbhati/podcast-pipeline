"""
Discord intake adapter.

Reads manually-shared URLs from the designated intake channel and
converts them into BriefingItems for pipeline processing.

Channel: #manual-intake (ID: 1480723611458342923)
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from .models import BriefingItem

_DISCORD_API = "https://discord.com/api/v10"
_URL_RE = re.compile(r"https?://[^\s>\"']+")


class DiscordIntakeAdapter:
    """Reads URL submissions from a Discord channel via the REST API."""

    def __init__(self, config: dict, run_logger: Any = None):
        self.token = config.get("bot_token", "")
        self.channel_id = config.get("intake_channel_id", "1480723611458342923")
        self.enabled = bool(self.token) and config.get("enabled", False)
        self.run_logger = run_logger

    def fetch_recent_items(
        self,
        since_hours: int = 24,
        since_dt: Optional[datetime] = None,
        until_dt: Optional[datetime] = None,
        source_date: Optional[str] = None,
    ) -> List[BriefingItem]:
        """
        Fetch messages from the intake channel and extract URLs/notes as BriefingItems.

        Filtering options:
        - since_hours: rolling lookback when explicit datetimes are not supplied
        - since_dt: lower bound (UTC-aware datetime preferred)
        - until_dt: upper bound (UTC-aware datetime preferred)
        - source_date: date label to stamp onto resulting manual items
        """
        if not self.enabled:
            print(
                f"  [Discord disabled] fetch_recent_items called."
                f"\n    channel: {self.channel_id}"
            )
            if self.run_logger:
                self.run_logger.event(
                    "provider_boundary_skipped",
                    provider="discord",
                    operation="fetch_recent_items",
                    status="skipped_disabled",
                    channel_id=self.channel_id,
                )
            return []

        try:
            if self.run_logger:
                with self.run_logger.boundary(
                    "discord",
                    "GET /channels/{channel_id}/messages",
                    channel_id=self.channel_id,
                    limit=100,
                    since_dt=since_dt.isoformat() if since_dt else None,
                    until_dt=until_dt.isoformat() if until_dt else None,
                ):
                    messages = self._fetch_messages(limit=100)
            else:
                messages = self._fetch_messages(limit=100)
            items = self._extract_items(
                messages,
                since_hours=since_hours,
                since_dt=since_dt,
                until_dt=until_dt,
                source_date=source_date,
            )
            if self.run_logger:
                self.run_logger.event(
                    "discord_items_extracted",
                    status="ok",
                    message_count=len(messages),
                    item_count=len(items),
                    channel_id=self.channel_id,
                )
            return items
        except urllib.error.HTTPError as e:
            print(f"  [Discord] HTTP {e.code}: {e.reason}")
            if self.run_logger:
                self.run_logger.event(
                    "discord_error",
                    status="error",
                    error_type="HTTPError",
                    http_status=e.code,
                    reason=e.reason,
                )
            return []
        except Exception as e:
            print(f"  [Discord] Error: {e}")
            if self.run_logger:
                self.run_logger.event(
                    "discord_error",
                    status="error",
                    error_type=type(e).__name__,
                    error=str(e),
                )
            return []

    def _fetch_messages(self, limit: int = 100) -> list:
        url = f"{_DISCORD_API}/channels/{self.channel_id}/messages?limit={limit}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bot {self.token}",
                "User-Agent": "NotebookLMPipeline/1.0 (briefing intake)",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _extract_items(
        self,
        messages: list,
        since_hours: int,
        since_dt: Optional[datetime] = None,
        until_dt: Optional[datetime] = None,
        source_date: Optional[str] = None,
    ) -> List[BriefingItem]:
        now_utc = datetime.now(tz=timezone.utc)
        cutoff = since_dt or (now_utc - timedelta(hours=since_hours))
        upper = until_dt
        stamped_date = source_date or now_utc.strftime("%Y-%m-%d")
        items: List[BriefingItem] = []

        for msg in messages:
            ts_raw = msg.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
                if upper and ts > upper:
                    continue
            except Exception:
                pass

            content: str = msg.get("content", "")
            if not content:
                continue

            urls = _URL_RE.findall(content)
            if not urls:
                if len(content) > 40:
                    items.append(
                        BriefingItem(
                            title=content[:120],
                            summary=content,
                            source="manual",
                            source_date=stamped_date,
                            url=None,
                            rating="Unknown",
                        )
                    )
                continue

            for url in urls:
                note = _URL_RE.sub("", content).strip()
                items.append(
                    BriefingItem(
                        title=note[:120] if note else url,
                        summary=note,
                        source="manual",
                        source_date=stamped_date,
                        url=url,
                        rating="Unknown",
                        is_strong=True,
                    )
                )

        return items

