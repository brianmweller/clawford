#!/usr/bin/env python3
"""activity-email-alert.py — Family Calendar (Mistress Mouse) email alerter.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`family-calendar:activity-email-check`. Runs the existing I/O script
`activity-email-check.py` to fetch recent activity-provider emails
(preschool, swim school, ballet studio), calls agents.shared.llm.infer
to classify each into {closure | cancellation | action | event | fyi |
none}, and sends one Telegram alert per non-"none" classification.

LLM classification (per the operator's logic-gate rule) is the right tier for
this cron: the inputs are free-form marketing-template emails where
pure keyword matching is brittle. We still use deterministic Python
for the prompt construction, response parsing, and per-item delivery.

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.llm import infer as llm_infer  # noqa: E402
from agents.shared.subprocess_helpers import (  # noqa: E402
    is_subprocess_error,
    run_json_script,
)
from agents.shared.telegram_api import resolve_credentials, send_message  # noqa: E402


WORKSPACE = Path(os.path.expanduser("~/.clawford/family-calendar-workspace"))
CACHE_DIR = WORKSPACE / "cache"
SCRIPTS_DIR = WORKSPACE / "scripts"
LAST_RUN_FILE = CACHE_DIR / "last-activity-email.json"

BOT_TOKEN_ENV = "FAMILYCAL_BOT_TOKEN"
SUBPROCESS_TIMEOUT_S = 90
LLM_TIMEOUT_S = 60

_PROMPT_TEMPLATE = """You classify activity-provider emails for the operator's family
calendar. Decide urgency and extract a one-line summary.

Urgency labels (pick exactly one):
- closure: school/activity is closed on a specific day (snow, staff training, illness)
- cancellation: a single class/event is cancelled, but the organization is still open
- action: RSVP required, form needed, supplies needed, payment due, deadline approaching
- event: a new event is announced (recital, parent night, field trip)
- fyi: general newsletter, schedule sharing, non-urgent info
- none: fundraising, generic marketing, or anything the operator does NOT need to act on

Summary: one short sentence the operator can scan in 2 seconds. Include the
date if the email names one. Do NOT repeat the source organization.

Return JSON ONLY, no markdown fences:
{{"urgency": "closure|cancellation|action|event|fyi|none", "summary": "..."}}

Email to classify:
From: {from_addr}
Subject: {subject}
Body:
{body}
"""


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    """Shim over agents.shared.subprocess_helpers.run_json_script so existing
    call sites keep working. Returns parsed JSON on success or
    {'__error__': ...} on any subprocess-level failure."""
    return run_json_script(str(SCRIPTS_DIR / script_name), *args, timeout=timeout)


def _strip_markdown_fence(text: str) -> str:
    """Remove leading/trailing ```json fences if the LLM emitted them
    despite json_mode. Matches the defensive unwrap in morning-edition.py.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


def classify_email(email: dict) -> tuple[dict, bool]:
    """Classify a single email via llm.infer. Returns
    (classification_dict, llm_failed_bool). On any failure — LLM down,
    malformed JSON, unrecognized urgency — the classification falls
    through as 'none' with an empty summary so the orchestrator can
    keep moving.
    """
    prompt = _PROMPT_TEMPLATE.format(
        from_addr=(email.get("from") or "").strip(),
        subject=(email.get("subject") or "").strip(),
        body=(email.get("body") or "")[:2500].strip(),
    )
    result = llm_infer(prompt, json_mode=True, timeout=LLM_TIMEOUT_S)
    if not getattr(result, "ok", False):
        return {"urgency": "none", "summary": ""}, True

    try:
        data = json.loads(_strip_markdown_fence(result.text or ""))
    except (json.JSONDecodeError, TypeError):
        return {"urgency": "none", "summary": ""}, True

    urgency = (data.get("urgency") or "none").strip().lower()
    if urgency not in ("closure", "cancellation", "action", "event", "fyi", "none"):
        urgency = "none"
    summary = (data.get("summary") or "").strip()
    return {"urgency": urgency, "summary": summary}, False


def format_message(email: dict, classification: dict) -> str | None:
    """Render a classified email as a Telegram alert line, or None to
    suppress. Prefixes match the original OpenClaw cron prompt."""
    urgency = classification.get("urgency", "none")
    summary = (classification.get("summary") or "").strip()
    source = (email.get("source") or "?").strip()

    if urgency in ("closure", "cancellation"):
        return f"\U0001f42d \u26a0\ufe0f {source}: {summary}"
    if urgency == "action":
        return f"\U0001f42d \U0001f4cb {source}: {summary}"
    if urgency in ("event", "fyi"):
        return f"\U0001f42d \U0001f4cc {source}: {summary}"
    return None


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)

    emails = _run_script("activity-email-check.py")

    # Propagate check-script failures instead of masking as 'no emails'.
    if is_subprocess_error(emails):
        error_msg = emails["__error__"]
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "error",
                    "error": error_msg,
                    "summary": f"activity-email-check failed: {error_msg[:120]}",
                },
                indent=2,
            ),
        )
        return {
            "status": "error",
            "error": error_msg,
            "alert": f"🐭 activity-email-check failed: {error_msg[:200]}",
        }

    if not isinstance(emails, list):
        emails = []

    classified = 0
    sent = 0
    llm_failures = 0

    if emails:
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        for email in emails:
            if not isinstance(email, dict):
                continue
            classified += 1
            result, failed = classify_email(email)
            if failed:
                llm_failures += 1
                continue
            msg = format_message(email, result)
            if msg is None:
                continue
            if send_message(token, chat_id, msg, silent=False):
                sent += 1

    _write_atomic(
        LAST_RUN_FILE,
        json.dumps(
            {
                "timestamp": now_utc.isoformat(),
                "status": "ok",
                "emails_seen": len(emails),
                "classified": classified,
                "sent": sent,
                "llm_failures": llm_failures,
                "summary": f"{sent} alert(s) sent" if sent else "no alerts",
            },
            indent=2,
        ),
    )

    return {
        "status": "ok",
        "emails_seen": len(emails),
        "classified": classified,
        "sent": sent,
        "llm_failures": llm_failures,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f42d activity-email-alert failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
