"""Structured JSONL logging for NotebookLM briefing pipeline runs.

The goal is post-failure inspection: each pipeline stage and each external/provider
boundary writes a durable event with timing, status, and sanitized details.
"""
from __future__ import annotations

import json
import os
import platform
import socket
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class PipelineRunLogger:
    """Append-only per-run JSONL logger.

    Events are deliberately plain JSON objects so humans and agents can inspect
    them with grep, jq, Python, or SQLite import later. Secrets are redacted by
    key name and long values are truncated to keep logs useful but bounded.
    """

    SECRET_KEYS = {
        "authorization",
        "bot_token",
        "cookie",
        "cookies",
        "password",
        "passwd",
        "secret",
        "token",
        "x-goog-authuser",
    }

    def __init__(self, root: Path, run_date: str, mode: str):
        self.root = Path(root)
        self.run_date = run_date
        self.mode = mode
        self.run_id = f"{run_date}_{mode}_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        self.path = self.root / "logs" / "pipeline-runs" / f"{self.run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def start(self, *, argv: list[str] | None = None, config_summary: dict[str, Any] | None = None) -> None:
        self.event(
            "run_start",
            status="started",
            run_date=self.run_date,
            mode=self.mode,
            argv=argv if argv is not None else sys.argv,
            config=config_summary or {},
            environment={
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "cwd": os.getcwd(),
            },
        )

    def finish(self, *, status: str, summary: str | None = None) -> None:
        self.event("run_finish", status=status, summary=summary)

    def event(self, event: str, **fields: Any) -> None:
        self._seq += 1
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "seq": self._seq,
            "run_id": self.run_id,
            "event": event,
            **self._sanitize(fields),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    @contextmanager
    def step(self, name: str, **fields: Any) -> Iterator[None]:
        start = time.perf_counter()
        self.event("step_start", step=name, status="started", **fields)
        try:
            yield
        except Exception as exc:
            self.event(
                "step_error",
                step=name,
                status="error",
                duration_ms=round((time.perf_counter() - start) * 1000),
                error_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            raise
        else:
            self.event(
                "step_finish",
                step=name,
                status="ok",
                duration_ms=round((time.perf_counter() - start) * 1000),
            )

    @contextmanager
    def boundary(self, provider: str, operation: str, **fields: Any) -> Iterator[None]:
        start = time.perf_counter()
        self.event(
            "provider_boundary_start",
            provider=provider,
            operation=operation,
            status="started",
            **fields,
        )
        try:
            yield
        except Exception as exc:
            self.event(
                "provider_boundary_error",
                provider=provider,
                operation=operation,
                status="error",
                duration_ms=round((time.perf_counter() - start) * 1000),
                error_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            raise
        else:
            self.event(
                "provider_boundary_finish",
                provider=provider,
                operation=operation,
                status="ok",
                duration_ms=round((time.perf_counter() - start) * 1000),
            )

    def summarize_value(self, value: Any, *, max_chars: int = 1200) -> Any:
        clean = self._sanitize(value)
        try:
            text = json.dumps(clean, ensure_ascii=False, default=str)
        except Exception:
            text = repr(clean)
        if len(text) <= max_chars:
            return clean
        return {
            "type": type(value).__name__,
            "truncated": True,
            "char_count": len(text),
            "preview": text[:max_chars],
        }

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                key = str(k)
                if any(secret in key.lower() for secret in self.SECRET_KEYS):
                    out[key] = "[REDACTED]"
                else:
                    out[key] = self._sanitize(v)
            return out
        if isinstance(value, (list, tuple)):
            if len(value) > 50:
                return [self._sanitize(v) for v in value[:50]] + [{"truncated_items": len(value) - 50}]
            return [self._sanitize(v) for v in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, str):
            if len(value) > 4000:
                return {"truncated": True, "char_count": len(value), "preview": value[:4000]}
            return value
        return value


def config_summary(config: dict[str, Any]) -> dict[str, Any]:
    """Small, secret-free config shape summary for run_start."""
    return {
        "discord_enabled": bool(config.get("discord", {}).get("enabled")),
        "discord_channel_id": config.get("discord", {}).get("intake_channel_id"),
        "notebooklm_enabled": bool(config.get("notebooklm", {}).get("enabled")),
        "notebooklm_profile_name": config.get("notebooklm", {}).get("profile_name"),
        "notebooklm_create_audio": config.get("notebooklm", {}).get("create_audio"),
        "raw_capture_enabled": config.get("raw_capture", {}).get("enabled", True),
        "audio_email_enabled": bool(config.get("notifications", {}).get("audio_email", {}).get("enabled")),
    }

