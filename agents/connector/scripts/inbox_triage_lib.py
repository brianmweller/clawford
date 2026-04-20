"""Pure triage helpers for inbox-triage.py.

classify_thread_for_triage: takes one Gmail thread (in the dict shape
returned by the Gmail threads().get() API) plus the email→slug map and
the operator's own addresses, and returns {status, ...} where status is one of:

  - queued                  — known sender, the operator hasn't replied last
  - skipped_empty           — thread has no messages (shouldn't happen)
  - skipped_brian_last      — the operator's message is the latest (already replied)
  - skipped_service         — latest sender matches service-account heuristic
  - skipped_unknown_sender  — latest sender not in email_to_slug

upsert_thread_in_queue: merge a single classified-thread result into an
existing triage queue dict. Used by inbox-triage.py's --thread-id mode,
which processes one thread in isolation (fired by gmail-push-listener
on a Pub/Sub notification) and must not clobber the rest of the queue
produced by the 30-min polling cron.

No network calls; the script layer fetches threads and feeds them in.
"""
from __future__ import annotations

from email.utils import parseaddr

from flux_import_lib import is_likely_service_account


def extract_email_from_header(raw: str | None) -> str:
    if not raw:
        return ""
    _name, addr = parseaddr(raw)
    addr = (addr or "").strip()
    # parseaddr is permissive — it returns "no" for "no angle brackets or at".
    # Only accept strings that look like email addresses.
    if "@" not in addr:
        return ""
    return addr


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "") or ""
    return ""


def latest_message(thread: dict) -> dict | None:
    msgs = thread.get("messages") or []
    if not msgs:
        return None
    return msgs[-1]


def classify_thread_for_triage(
    thread: dict,
    *,
    operator_emails: set[str],
    email_to_slug: dict[str, str],
) -> dict:
    thread_id = thread.get("id", "")
    latest = latest_message(thread)
    if latest is None:
        return {"status": "skipped_empty", "thread_id": thread_id}

    from_raw = _header(latest, "From")
    from_email = extract_email_from_header(from_raw).lower()
    subject = _header(latest, "Subject")
    date = _header(latest, "Date")
    message_id = _header(latest, "Message-ID") or _header(latest, "Message-Id")
    snippet = latest.get("snippet", "")

    base = {
        "thread_id": thread_id,
        "from_email": from_email,
        "from_header": from_raw,
        "subject": subject,
        "date": date,
        "snippet": snippet,
        "in_reply_to_message_id": message_id,
    }

    if not from_email:
        return {**base, "status": "skipped_no_from"}

    if from_email in {a.lower() for a in operator_emails}:
        return {**base, "status": "skipped_brian_last"}

    if is_likely_service_account(from_email, from_raw):
        return {**base, "status": "skipped_service"}

    slug = email_to_slug.get(from_email)
    if not slug:
        return {**base, "status": "skipped_unknown_sender"}

    return {**base, "slug": slug, "status": "queued"}


def upsert_thread_in_queue(queue: dict | None, classified: dict) -> dict:
    """Return a new queue dict with `classified` merged in.

    Rules:
      * Any existing queued entry matching classified["thread_id"] is
        removed first (fresh classification wins).
      * If classified["status"] == "queued", the entry is appended.
      * Otherwise (skipped_*), the entry is NOT added — a non-queued
        thread is simply absent from the queue.
      * Other entries in the queue are preserved untouched (so the
        30-min polling scan's output isn't clobbered by a single-
        thread push-listener invocation).

    Safe on an empty/missing queue — pass None or {} to start fresh.
    """
    base = queue if isinstance(queue, dict) else {}
    existing = list(base.get("queued") or [])

    tid = classified.get("thread_id")
    filtered = [e for e in existing if e.get("thread_id") != tid] if tid else existing

    if classified.get("status") == "queued":
        # Pass through only the fields auto-compose reads; stripping
        # transient classifier diagnostics keeps the queue tidy.
        entry = {
            k: classified[k] for k in (
                "thread_id", "slug", "from_email", "from_header",
                "subject", "date", "snippet", "in_reply_to_message_id",
            ) if k in classified
        }
        filtered.append(entry)

    return {**base, "queued": filtered}
