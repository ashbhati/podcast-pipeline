"""
NotebookLM publish adapter backed by `notebooklm-mcp-cli` / `notebooklm_tools`.

This implementation uses the installed jacob-bd/notebooklm-mcp-cli project as
our NotebookLM integration layer.

Flow:
  1. Load NotebookLM auth profile created by `nlm login`
  2. Create a notebook for the AM/PM briefing pack
  3. Upload the generated markdown file as a source
  4. Optionally request an audio overview

If auth is missing, the adapter fails soft and prints the exact next action.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


DEFAULT_AUDIO_FOCUS_PROMPT = (
    "Generate a high-intensity, operator-grade technical briefing, not a generic recap. "
    "Target roughly 30-40 minutes of crisp, dense insight with zero fluff and minimal repetition. "
    "Prioritize implementation reality: architecture choices, benchmarks, tooling constraints, failure modes, and tradeoffs. "
    "When possible, compare claims across sources and call out evidence quality, uncertainty, and hype versus signal. "
    "Focus on concrete technical mechanisms, measurable evidence, enterprise safety/governance implications, "
    "second-order product/GTM/org effects, and what an advanced AI operator should do in the next 24-72 hours."
)


class NotebookLMAdapter:
    """Adapter for publishing briefing packs to NotebookLM via notebooklm_tools."""

    def __init__(self, config: dict, run_logger: Any = None):
        self.config = config
        self.run_logger = run_logger
        self.enabled = config.get("enabled", False)
        self.notebook_url = config.get("notebook_url", "https://notebooklm.google.com")
        self.default_notebook = config.get("default_notebook_name", "AI Daily Briefing")
        self.profile_name = config.get("profile_name", "default")
        self.create_audio = config.get("create_audio", True)
        self.audio_required = bool(config.get("audio_required", False))
        self.public_by_default = bool(config.get("public_by_default", True))
        self.audio_format = str(config.get("audio_format", "deep_dive")).lower()
        self.audio_length = str(config.get("audio_length", "long")).lower()
        self.audio_focus_prompt = str(config.get("audio_focus_prompt", DEFAULT_AUDIO_FOCUS_PROMPT)).strip()
        self.auto_delete_old_generated_notebooks = bool(
            config.get("auto_delete_old_generated_notebooks", False)
        )
        self.notebook_retention_keep_latest = int(
            config.get("notebook_retention_keep_latest", 80) or 80
        )
        self.generated_notebook_prefixes = tuple(
            config.get("generated_notebook_prefixes") or ["AI Briefing "]
        )

    def publish_pack(
        self,
        pack_path: Path,
        notebook_name: Optional[str] = None,
    ) -> tuple[str, str]:
        """
        Publish a briefing pack markdown file to NotebookLM.

        Returns (notebook_url, audio_status) on success.
        Raises RuntimeError with a source-specific reason on failure.
        """
        name = notebook_name or self.default_notebook

        if not self.enabled:
            raise RuntimeError(
                f"NotebookLM publishing disabled in config for {pack_path.name}"
            )

        if not pack_path.exists():
            raise RuntimeError(f"NotebookLM source file missing: {pack_path}")

        client = self._load_client()
        self._attach_rpc_logging(client)

        try:
            if self.run_logger:
                self.run_logger.event(
                    "notebooklm_publish_start",
                    status="started",
                    notebook_name=name,
                    pack_path=str(pack_path),
                    pack_bytes=pack_path.stat().st_size,
                    public_by_default=self.public_by_default,
                    create_audio=self.create_audio,
                )

            notebook = self._create_notebook_with_quota_recovery(client, name)
            if not notebook or not getattr(notebook, "id", None):
                if self.run_logger:
                    self.run_logger.event(
                        "notebooklm_create_notebook_invalid_result",
                        status="error",
                        notebook_name=name,
                        result=self.run_logger.summarize_value(notebook),
                    )
                raise RuntimeError(
                    f"NotebookLM notebook creation failed for '{name}'"
                    f"{self._notebook_quota_hint(client)}"
                )

            notebook_id = notebook.id
            notebook_url = f"{self.notebook_url.rstrip('/')}/notebook/{notebook_id}"
            print(f"  [NotebookLM] notebook created: {name} ({notebook_id})")
            if self.run_logger:
                self.run_logger.event(
                    "notebooklm_notebook_created",
                    status="ok",
                    notebook_name=name,
                    notebook_id=notebook_id,
                    notebook_url=notebook_url,
                )

            if self.public_by_default:
                if self.run_logger:
                    with self.run_logger.boundary("notebooklm", "set_public_access", notebook_id=notebook_id):
                        public_url = client.set_public_access(notebook_id, True)
                    with self.run_logger.boundary("notebooklm", "get_share_status", notebook_id=notebook_id):
                        share_status = client.get_share_status(notebook_id)
                else:
                    public_url = client.set_public_access(notebook_id, True)
                    share_status = client.get_share_status(notebook_id)
                if share_status and share_status.is_public:
                    notebook_url = share_status.public_link or public_url or notebook_url
                    print(f"  [NotebookLM] public access enabled: {notebook_url}")
                    if self.run_logger:
                        self.run_logger.event(
                            "notebooklm_public_access_enabled",
                            status="ok",
                            notebook_id=notebook_id,
                            notebook_url=notebook_url,
                        )
                else:
                    if self.run_logger:
                        self.run_logger.event(
                            "notebooklm_public_access_invalid_status",
                            status="error",
                            notebook_id=notebook_id,
                            share_status=self.run_logger.summarize_value(share_status),
                            public_url=public_url,
                        )
                    raise RuntimeError("failed to enable public access for notebook")

            if self.run_logger:
                with self.run_logger.boundary("notebooklm", "add_file", notebook_id=notebook_id, pack_path=str(pack_path)):
                    source_result = client.add_file(notebook_id, str(pack_path), wait=True)
            else:
                source_result = client.add_file(notebook_id, str(pack_path), wait=True)
            if not source_result or not source_result.get("id"):
                if self.run_logger:
                    self.run_logger.event(
                        "notebooklm_source_upload_invalid_result",
                        status="error",
                        notebook_id=notebook_id,
                        result=self.run_logger.summarize_value(source_result),
                    )
                raise RuntimeError("source upload did not return a valid source id")
            print(f"  [NotebookLM] source uploaded: {pack_path.name} (source_id={source_result.get('id')})")
            if self.run_logger:
                self.run_logger.event(
                    "notebooklm_source_uploaded",
                    status="ok",
                    notebook_id=notebook_id,
                    source_id=source_result.get("id"),
                    source_result=self.run_logger.summarize_value(source_result),
                )

            audio_status = "disabled"
            if self.create_audio:
                audio_kwargs = {
                    "notebook_id": notebook_id,
                    "source_ids": [source_result["id"]],
                    "format_code": self._map_audio_format(),
                    "length_code": self._map_audio_length(),
                    "focus_prompt_char_count": len(self.audio_focus_prompt),
                }
                if self.run_logger:
                    with self.run_logger.boundary("notebooklm", "create_audio_overview", **audio_kwargs):
                        audio_result = client.create_audio_overview(
                            notebook_id,
                            source_ids=[source_result["id"]],
                            format_code=audio_kwargs["format_code"],
                            length_code=audio_kwargs["length_code"],
                            focus_prompt=self.audio_focus_prompt,
                        )
                else:
                    audio_result = client.create_audio_overview(
                        notebook_id,
                        source_ids=[source_result["id"]],
                        format_code=audio_kwargs["format_code"],
                        length_code=audio_kwargs["length_code"],
                        focus_prompt=self.audio_focus_prompt,
                    )
                if audio_result and audio_result.get("artifact_id"):
                    status = audio_result.get("status", "unknown")
                    audio_status = "requested"
                    print(
                        "  [NotebookLM] audio overview requested"
                        f" (artifact_id={audio_result.get('artifact_id')}, status={status})"
                    )
                    if self.run_logger:
                        self.run_logger.event(
                            "notebooklm_audio_requested",
                            status="ok",
                            notebook_id=notebook_id,
                            audio_result=self.run_logger.summarize_value(audio_result),
                        )
                elif self.audio_required:
                    if self.run_logger:
                        self.run_logger.event(
                            "notebooklm_audio_invalid_result",
                            status="error",
                            notebook_id=notebook_id,
                            audio_result=self.run_logger.summarize_value(audio_result),
                        )
                    raise RuntimeError("audio overview trigger failed (no artifact id returned)")
                else:
                    audio_status = "quota_reached"
                    print("  [NotebookLM] audio overview request did not return artifact id (likely quota reached)")
                    if self.run_logger:
                        self.run_logger.event(
                            "notebooklm_audio_quota_or_empty_result",
                            status="quota_reached",
                            notebook_id=notebook_id,
                            audio_result=self.run_logger.summarize_value(audio_result),
                        )

            if self.run_logger:
                self.run_logger.event(
                    "notebooklm_publish_finish",
                    status="ok",
                    notebook_id=notebook_id,
                    notebook_url=notebook_url,
                    audio_status=audio_status,
                )
            return notebook_url, audio_status
        except Exception as exc:
            print(f"  [NotebookLM] publish failed: {exc}")
            if self.run_logger:
                self.run_logger.event(
                    "notebooklm_publish_error",
                    status="error",
                    notebook_name=name,
                    pack_path=str(pack_path),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            raise RuntimeError(f"NotebookLM publish failed: {str(exc).strip()}") from exc

    def _create_notebook_with_quota_recovery(self, client: Any, name: str) -> Any:
        """Create a notebook, optionally freeing generated-notebook quota first.

        NotebookLM currently returns an empty RPC result when the account is at
        its notebook limit. The upstream client turns that into ``None``, which
        used to make the pipeline fail with a generic create error. We keep the
        default safe (no deletes), but support an explicit retention policy for
        generated briefing notebooks so the scheduled publisher can recover once
        the operator enables it.
        """
        notebook = self._create_notebook(client, name)
        if notebook or not self.auto_delete_old_generated_notebooks:
            return notebook

        notebooks = self._safe_list_notebooks(client)
        if len(notebooks) < 100:
            return notebook

        deleted = self._delete_old_generated_notebooks(client, notebooks)
        if deleted <= 0:
            return notebook

        if self.run_logger:
            self.run_logger.event(
                "notebooklm_create_retry_after_retention_cleanup",
                status="started",
                notebook_name=name,
                deleted_count=deleted,
            )
        return self._create_notebook(client, name)

    def _create_notebook(self, client: Any, name: str) -> Any:
        if self.run_logger:
            with self.run_logger.boundary("notebooklm", "create_notebook", notebook_name=name):
                return client.create_notebook(name)
        return client.create_notebook(name)

    def _safe_list_notebooks(self, client: Any) -> list[Any]:
        try:
            if self.run_logger:
                with self.run_logger.boundary("notebooklm", "list_notebooks_for_quota"):
                    notebooks = client.list_notebooks()
            else:
                notebooks = client.list_notebooks()
            return list(notebooks or [])
        except Exception as exc:
            if self.run_logger:
                self.run_logger.event(
                    "notebooklm_list_notebooks_for_quota_error",
                    status="error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            return []

    def _notebook_quota_hint(self, client: Any) -> str:
        notebooks = self._safe_list_notebooks(client)
        if len(notebooks) >= 100:
            generated = [nb for nb in notebooks if self._is_generated_notebook(nb)]
            return (
                f"; NotebookLM account appears to be at the 100-notebook limit "
                f"({len(generated)} generated briefing notebooks found). "
                "Delete old generated notebooks or enable "
                "notebooklm.auto_delete_old_generated_notebooks with a retention limit."
            )
        return ""

    def _is_generated_notebook(self, notebook: Any) -> bool:
        title = str(getattr(notebook, "title", "") or "")
        return any(title.startswith(prefix) for prefix in self.generated_notebook_prefixes)

    def _delete_old_generated_notebooks(self, client: Any, notebooks: list[Any]) -> int:
        generated = [nb for nb in notebooks if self._is_generated_notebook(nb)]
        # FIFO retention: delete the oldest generated notebooks by creation time,
        # not by last-modified time. Older notebooks can be touched later by sharing,
        # audio polling, or metadata refreshes; creation time reflects queue order.
        generated.sort(key=lambda nb: getattr(nb, "created_at", None) or getattr(nb, "modified_at", None) or "")
        delete_count = max(0, len(generated) - self.notebook_retention_keep_latest)
        victims = generated[:delete_count]
        deleted = 0
        for nb in victims:
            notebook_id = getattr(nb, "id", None)
            if not notebook_id:
                continue
            title = getattr(nb, "title", "")
            try:
                if self.run_logger:
                    with self.run_logger.boundary(
                        "notebooklm",
                        "delete_old_generated_notebook",
                        notebook_id=notebook_id,
                        title=title,
                    ):
                        ok = client.delete_notebook(notebook_id)
                else:
                    ok = client.delete_notebook(notebook_id)
                if ok:
                    deleted += 1
            except Exception as exc:
                if self.run_logger:
                    self.run_logger.event(
                        "notebooklm_delete_old_generated_notebook_error",
                        status="error",
                        notebook_id=notebook_id,
                        title=title,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
        if self.run_logger:
            self.run_logger.event(
                "notebooklm_generated_notebook_retention_cleanup",
                status="ok" if deleted else "skipped",
                generated_count=len(generated),
                keep_latest=self.notebook_retention_keep_latest,
                deleted_count=deleted,
            )
        return deleted

    def get_audio_url(self, notebook_id: str) -> Optional[str]:
        """
        NotebookLM audio URLs are not yet normalized here.
        Returns the notebook URL for now.
        """
        return f"{self.notebook_url.rstrip('/')}/notebook/{notebook_id}"

    def _attach_rpc_logging(self, client) -> None:
        """Wrap notebooklm_tools RPC calls to capture raw boundary outcomes.

        This is intentionally best-effort and does not change client behavior.
        It records sanitized params and result summaries so failures like an empty
        create_notebook RPC result can be diagnosed after the run.
        """
        if not self.run_logger or getattr(client, "_pipeline_rpc_logging_attached", False):
            return

        original = getattr(client, "_call_rpc", None)
        if not callable(original):
            self.run_logger.event(
                "notebooklm_rpc_logging_unavailable",
                status="skipped",
                reason="client has no callable _call_rpc",
            )
            return

        try:
            from notebooklm_tools.core import client as client_mod
            rpc_names = getattr(client_mod, "RPC_NAMES", {})
        except Exception:
            rpc_names = {}

        run_logger = self.run_logger

        def logged_call_rpc(rpc_id, params, path="/", timeout=None, *args, **kwargs):
            operation = rpc_names.get(rpc_id, "unknown_rpc")
            with run_logger.boundary(
                "notebooklm_rpc",
                operation,
                rpc_id=rpc_id,
                path=path,
                timeout=timeout,
                params=run_logger.summarize_value(params),
            ):
                result = original(rpc_id, params, path, timeout, *args, **kwargs)
            run_logger.event(
                "notebooklm_rpc_result",
                status="ok" if result is not None else "empty",
                rpc_id=rpc_id,
                operation=operation,
                result=run_logger.summarize_value(result),
            )
            return result

        client._call_rpc = logged_call_rpc
        client._pipeline_rpc_logging_attached = True
        self.run_logger.event("notebooklm_rpc_logging_attached", status="ok")

    def _load_client(self):
        try:
            from notebooklm_tools.core.auth import AuthManager
            from notebooklm_tools.core.client import NotebookLMClient
        except Exception as exc:
            print(
                "  [NotebookLM] notebooklm-mcp-cli package not available. "
                "Install with: pip install notebooklm-mcp-cli"
            )
            print(f"    detail: {exc}")
            raise RuntimeError("NotebookLM client package not available") from exc

        try:
            if self.run_logger:
                with self.run_logger.boundary("notebooklm_auth", "load_profile", profile_name=self.profile_name):
                    auth = AuthManager(profile_name=self.profile_name)
                    profile = auth.load_profile()
                with self.run_logger.boundary("notebooklm_auth", "init_client", profile_name=self.profile_name):
                    client = NotebookLMClient(profile.cookies)
            else:
                auth = AuthManager(profile_name=self.profile_name)
                profile = auth.load_profile()
                client = NotebookLMClient(profile.cookies)
            return client
        except Exception as exc:
            print(
                f"  [NotebookLM] auth profile '{self.profile_name}' is not ready."
                "\n    Run: nlm login"
                "\n    Then re-run the pipeline."
            )
            print(f"    detail: {exc}")
            raise RuntimeError(
                f"NotebookLM auth profile '{self.profile_name}' is not ready; run nlm login"
            ) from exc

    def _map_audio_format(self) -> int:
        try:
            from notebooklm_tools.core.client import NotebookLMClient
        except Exception:
            return 1

        mapping = {
            "deep_dive": NotebookLMClient.AUDIO_FORMAT_DEEP_DIVE,
            "brief": NotebookLMClient.AUDIO_FORMAT_BRIEF,
            "critique": NotebookLMClient.AUDIO_FORMAT_CRITIQUE,
            "debate": NotebookLMClient.AUDIO_FORMAT_DEBATE,
        }
        return mapping.get(self.audio_format, NotebookLMClient.AUDIO_FORMAT_DEEP_DIVE)

    def _map_audio_length(self) -> int:
        try:
            from notebooklm_tools.core.client import NotebookLMClient
        except Exception:
            return 3

        mapping = {
            "short": NotebookLMClient.AUDIO_LENGTH_SHORT,
            "default": NotebookLMClient.AUDIO_LENGTH_DEFAULT,
            "long": NotebookLMClient.AUDIO_LENGTH_LONG,
        }
        return mapping.get(self.audio_length, NotebookLMClient.AUDIO_LENGTH_LONG)

