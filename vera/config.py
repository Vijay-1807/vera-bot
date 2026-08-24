"""Runtime configuration: submission identity plus engine tunables.

Identity is read from (in order of precedence) environment variables, then
``identity.json`` beside the project root, then obvious placeholders. Anything
still marked TODO is surfaced loudly at startup and in ``/v1/metadata`` so it
cannot be missed before submission.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import __version__

ROOT = Path(__file__).resolve().parent.parent
IDENTITY_FILE = ROOT / "identity.json"

TODO = "TODO"


def _load_identity_file() -> dict:
    try:
        with open(IDENTITY_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_FILE = _load_identity_file()


def _setting(env_key: str, file_key: str, fallback: str) -> str:
    value = os.environ.get(env_key)
    if value is None:
        value = _FILE.get(file_key)
    if value is None or str(value).strip() == "":
        return fallback
    return str(value).strip()


TEAM_NAME = _setting("VERA_TEAM_NAME", "team_name", f"{TODO}: set VERA_TEAM_NAME")
CONTACT_EMAIL = _setting("VERA_CONTACT_EMAIL", "contact_email", f"{TODO}: set VERA_CONTACT_EMAIL")
SUBMITTED_AT = _setting("VERA_SUBMITTED_AT", "submitted_at", "2026-08-24T00:00:00Z")
PUBLIC_URL = _setting("VERA_PUBLIC_URL", "public_url", "")


def _team_members() -> list:
    raw = os.environ.get("VERA_TEAM_MEMBERS")
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    members = _FILE.get("team_members")
    if isinstance(members, list) and members:
        return [str(m).strip() for m in members if str(m).strip()]
    if isinstance(members, str) and members.strip():
        return [part.strip() for part in members.split(",") if part.strip()]
    return [f"{TODO}: set VERA_TEAM_MEMBERS"]


TEAM_MEMBERS = _team_members()

# Honest declaration: the request path runs no model. See README for the rationale.
MODEL = _setting("VERA_MODEL", "model",
                 "none — deterministic rule engine (no LLM at request time)")
APPROACH = (
    "Provenance-tracked fact sheet built from the four pushed contexts, per-trigger-kind "
    "playbooks that dispatch on trigger.kind and degrade gracefully on sparse payloads, "
    "urgency x relevance x merchant-state scoring with suppression and frequency caps for "
    "send/hold decisions, and a pre-send validator that rejects any number without "
    "provenance, any second CTA, taboo vocabulary, internal jargon, URLs and repeats. "
    "Fully deterministic: identical context in, identical bytes out."
)

VERSION = __version__

# ---------------------------------------------------------------- engine tunables

# Harness contract (challenge-testing-brief.md sections 3 and 6).
MAX_ACTIONS_PER_TICK = 20

# Restraint. The rubric rewards silence over noise, so these are deliberately tight.
MAX_SENDS_PER_MERCHANT_PER_TEST = 3        # across the whole 60-minute window
MIN_MINUTES_BETWEEN_MERCHANT_SENDS = 20    # simulated-clock gap between touches
MAX_CUSTOMER_SENDS_PER_TEST = 1            # one outbound per customer, ever
SCORE_FLOOR = 0.30                         # below this a trigger is not worth a message
OPT_OUT_SUPPRESSION_DAYS = 30              # after a hostile exit or explicit opt-out

# Back-off ladder for merchant auto-replies (seconds).
AUTOREPLY_BACKOFF_SECONDS = 14400          # 4h after the first canned reply
AUTOREPLY_LONG_BACKOFF_SECONDS = 86400     # 24h after the second
POST_SEND_QUIET_SECONDS = 86400            # after the merchant acts, stop pushing

# Composition shape (word counts, soft targets used by the validator).
TARGET_MERCHANT_WORDS = (38, 78)
TARGET_CUSTOMER_WORDS = (30, 65)

# Server.
HOST = os.environ.get("VERA_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("VERA_PORT") or 8080)
MAX_BODY_BYTES = int(os.environ.get("VERA_MAX_BODY_BYTES", 8 * 1024 * 1024))
LOG_LEVEL = os.environ.get("VERA_LOG_LEVEL", "info").lower()


def identity_warnings() -> list:
    """Human-readable warnings for anything left unset before submission."""
    warnings = []
    if TEAM_NAME.startswith(TODO):
        warnings.append("team_name is unset (set VERA_TEAM_NAME or identity.json)")
    if CONTACT_EMAIL.startswith(TODO):
        warnings.append("contact_email is unset (set VERA_CONTACT_EMAIL or identity.json)")
    if any(str(member).startswith(TODO) for member in TEAM_MEMBERS):
        warnings.append("team_members is unset (set VERA_TEAM_MEMBERS or identity.json)")
    return warnings


def metadata() -> dict:
    """Payload for GET /v1/metadata."""
    payload = {
        "team_name": TEAM_NAME,
        "team_members": list(TEAM_MEMBERS),
        "model": MODEL,
        "approach": APPROACH,
        "contact_email": CONTACT_EMAIL,
        "version": VERSION,
        "submitted_at": SUBMITTED_AT,
        "deterministic": True,
        "endpoints": [
            "GET /v1/healthz",
            "GET /v1/metadata",
            "POST /v1/context",
            "POST /v1/tick",
            "POST /v1/reply",
            "POST /v1/teardown",
        ],
    }
    if PUBLIC_URL:
        payload["public_url"] = PUBLIC_URL
    warnings = identity_warnings()
    if warnings:
        payload["config_warnings"] = warnings
    return payload
