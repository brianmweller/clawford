"""scan_fields — reusable wire-in helper for inbound-content scanning.

Ingesting scripts (meeting-prep, calendar-scan, article-ingest,
inbox-triage, etc.) call `scan_fields()` to check a bundle of
externally-sourced strings at once, quarantine anything the scanner
flags, and get back a sanitized dict + warning list suitable for the
script's JSON envelope.

Modes (env var `CLAWFORD_INBOUND_SCANNER_MODE`):

  - "warn" (default) — pass through the original field values, but
    emit warnings in the envelope. Use during initial rollout so
    false-positives don't break the operator's week.
  - "enforce" — replace `block`ed field values with a one-line
    placeholder naming the pattern that tripped. Use after a week
    of `warn` review shows the false-positive rate is acceptable.

Quarantine: every non-`allow` scan result is appended to
`<workspace>/cache/quarantine/inbound-<ISO8601>.jsonl`. No rotation —
if this file gets huge, you have bigger problems than disk.

Example use (from a calendar ingester):

    from agents.shared.scan_fields import scan_fields

    sanitized, warnings = scan_fields(
        fields={
            "title": event["summary"],
            "description": event.get("description", ""),
            **{f"attendee_{i}_name": a.get("name", "") for i, a in enumerate(event["attendees"])},
        },
        source_type="calendar",
        source_id=event["id"],
        workspace=Path("~/.clawford/meetings-coach-workspace").expanduser(),
    )
    # sanitized["title"] is safe to use. warnings is a list[dict] for
    # the envelope: {"field": "title", "status": "block", ...}
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Local-dir sys.path shim so tests that set CLAWFORD_PROMPTS_DIR don't
# also need to rewrite imports. The scanner and its patterns live
# alongside this helper.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from inbound_scanner import scan_inbound  # type: ignore  # noqa: E402


MODE_ENV_VAR = "CLAWFORD_INBOUND_SCANNER_MODE"
DEFAULT_MODE = "warn"
ALLOWED_MODES = ("warn", "enforce")


def _current_mode() -> str:
    raw = (os.environ.get(MODE_ENV_VAR) or DEFAULT_MODE).strip().lower()
    if raw not in ALLOWED_MODES:
        return DEFAULT_MODE
    return raw


@dataclass
class FieldWarning:
    """One row in the `scan_warnings` envelope list."""

    field: str
    status: str                  # "quarantine" | "block"
    flagged_pattern: str | None  # regex label if a block
    reason: str
    source_type: str
    source_id: str
    mode: str                    # the mode that was in effect

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "status": self.status,
            "flagged_pattern": self.flagged_pattern,
            "reason": self.reason,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "mode": self.mode,
        }


def _block_placeholder(field_name: str, label: str | None) -> str:
    label_display = label or "suspicious-pattern"
    return f"⚠️ [blocked by inbound-scan: {label_display}]"


def _quarantine_path(workspace: Path) -> Path:
    quarantine_dir = workspace / "cache" / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return quarantine_dir / f"inbound-{today}.jsonl"


def _append_quarantine(workspace: Path, record: dict) -> None:
    """Append one JSONL record. Best-effort — never raises."""
    try:
        path = _quarantine_path(workspace)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Disk full, permissions, etc. Don't break the script over
        # a missed audit-log line — but do surface it via stderr.
        print(
            f"[scan_fields] WARN: failed to write quarantine record: {record!r}",
            file=sys.stderr,
        )


def scan_fields(
    fields: dict[str, str | None],
    *,
    source_type: str,
    source_id: str,
    workspace: Path,
    mode: str | None = None,
) -> tuple[dict[str, str], list[dict]]:
    """Scan every value in `fields`; return (sanitized_dict, warnings_list).

    `fields` keys are arbitrary script-chosen names ("title",
    "description", "attendee_0_name", etc.) and values are the raw
    strings pulled from the ingest source. `None` values pass through
    as empty strings. Non-string values are coerced via `str()` so
    the helper is drop-in over ad-hoc dicts.

    The returned `sanitized_dict` has the same keys. In `warn` mode,
    values are unchanged. In `enforce` mode, `block`ed values are
    replaced by a one-line placeholder; `quarantine`d values are
    replaced by an empty string (length overflow is not safe to feed
    into the LLM either way).

    `warnings_list` is a JSON-safe list suitable for inclusion under
    a top-level `scan_warnings` envelope key. Empty list means
    everything scanned clean.

    Every non-`allow` scan result also gets appended to a JSONL file
    under `<workspace>/cache/quarantine/inbound-<date>.jsonl` for
    later operator review.
    """
    active_mode = (mode or _current_mode()).lower()
    if active_mode not in ALLOWED_MODES:
        active_mode = DEFAULT_MODE

    sanitized: dict[str, str] = {}
    warnings: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for field_name, raw in fields.items():
        if raw is None:
            sanitized[field_name] = ""
            continue
        text = raw if isinstance(raw, str) else str(raw)

        result = scan_inbound(
            text,
            source_type=source_type,
            source_id=source_id,
        )

        if result.status == "allow":
            sanitized[field_name] = text
            continue

        warning = FieldWarning(
            field=field_name,
            status=result.status,
            flagged_pattern=result.flagged_pattern,
            reason=result.reason,
            source_type=source_type,
            source_id=source_id,
            mode=active_mode,
        )
        warnings.append(warning.as_dict())

        _append_quarantine(
            workspace,
            {
                "timestamp": now_iso,
                "field": field_name,
                "source_type": source_type,
                "source_id": source_id,
                "status": result.status,
                "flagged_pattern": result.flagged_pattern,
                "reason": result.reason,
                "mode": active_mode,
                "text": text,  # full content so operator can inspect
            },
        )

        # Sanitize or pass through based on mode.
        if active_mode == "enforce":
            if result.status == "block":
                sanitized[field_name] = _block_placeholder(
                    field_name, result.flagged_pattern
                )
            else:  # quarantine (length overflow etc.)
                sanitized[field_name] = ""
        else:  # warn
            sanitized[field_name] = text

    return sanitized, warnings
