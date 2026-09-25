# Podcast automation reference

This document describes the repository's generic pipeline. Configure schedules,
destinations, credentials, local paths, and NotebookLM profile details in the
private runtime; they are intentionally omitted here.

## Pipeline stages

1. Read the selected AM or PM briefing files and optional, locally configured
   Discord intake.
2. Parse stories, normalize and deduplicate URLs, and classify each item.
3. Build a separate Markdown and JSON pack for each requested edition.
4. Stop before NotebookLM if an edition has fewer than the configured minimum
   stories (default: two). The run log records `insufficient_current_evidence`,
   the edition, and the observed count.
5. Capture linked article content. Prefer visible article text; when a page is
   mostly a client-rendered shell, use a sufficiently long JSON-LD `articleBody`
   when present. Record the selected extraction method and text length.
6. Publish eligible packs, request audio, and continue through the configured
   audio and feed publication stages.

## Local configuration

Copy `config.example.json` to `config.json`. Supply local values for credentials,
destinations, paths, and provider profiles. Discord intake remains disabled
unless both a token and channel ID are configured explicitly.

Do not commit `config.json`, credentials, browser profiles, state databases,
logs, generated briefing packs, or audio files. See `SECURITY.md` for the public
repository's exclusion rules.

## Run evidence

Use the local run log and state database to distinguish preparation, NotebookLM
submission, audio completion, and feed publication. A successful earlier stage
does not imply later stages completed. Do not retry an operation when its
external outcome is uncertain; reconcile the provider state first.

## Scope

This Python application consumes prepared briefing files. It does not implement
the separate live-source discovery and editorial ranking service used by some
OpenClaw deployments. Source registries, account-specific schedules, Discord
IDs, private ledgers, and machine-local runtime settings are not part of this
public reference.
