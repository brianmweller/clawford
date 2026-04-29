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

from datetime import datetime, timezone

from flux_import_lib import is_likely_service_account
from people_promote_lib import _derive_name_and_slug, promote_to_people_brain
from recruiter_detector_lib import AMBIGUOUS_DOMAINS, RECRUITER_DOMAINS, is_likely_recruiter


def _is_recruiter_domain(email: str) -> bool:
    """True if the email's domain matches a known ATS / recruiting
    platform OR an ambiguous-but-recruiter-adjacent domain (LinkedIn
    InMail). Used to exempt such emails from the service-account filter
    so the recruiter detector gets a chance to classify on content —
    ambiguous-domain hits without recruiter phrasing fall through to
    skipped_unknown_sender downstream."""
    if not email or "@" not in email:
        return False
    domain = email.partition("@")[2].lower()
    for rd in RECRUITER_DOMAINS:
        if domain == rd or domain.endswith("." + rd):
            return True
    for ad in AMBIGUOUS_DOMAINS:
        if domain == ad or domain.endswith("." + ad):
            return True
    return False


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


def _brian_in_prior_thread_history(thread: dict, operator_emails: set[str]) -> bool:
    """True if any message in the thread BEFORE the latest has a From
    header matching the operator's addresses.

    The thread-continuity backstop: once the operator engages on a thread,
    future inbounds from any sender on it should draft regardless of
    people-brain membership. Excludes the latest message because that's
    the one being classified (and an outbound-from-the operator latest is
    already caught by skipped_brian_last upstream)."""
    msgs = thread.get("messages") or []
    if len(msgs) < 2:
        return False
    brian_lower = {a.lower() for a in operator_emails}
    for m in msgs[:-1]:
        from_email = extract_email_from_header(_header(m, "From")).lower()
        if from_email and from_email in brian_lower:
            return True
    return False


def classify_thread_for_triage(
    thread: dict,
    *,
    operator_emails: set[str],
    email_to_slug: dict[str, str],
    rejected_recruiters: set[str] | None = None,
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

    # Rejected-recruiter short-circuit: once the operator taps 🚫 Not a fit on a
    # prior cold-recruiter FYI, future inbounds from the same sender
    # don't re-run the detector or re-draft. Checked before the
    # service-account filter so a rejected ATS sender doesn't waste
    # detector cycles.
    if rejected_recruiters and from_email in rejected_recruiters:
        return {**base, "status": "skipped_rejected_recruiter"}

    # Recruiter-domain exemption: ATS / retained-search platforms often
    # send from no-reply@ prefixes that trip the service-account
    # filter. Let those fall through to the unknown-sender branch where
    # the recruiter detector catches them.
    if is_likely_service_account(from_email, from_raw) and not _is_recruiter_domain(from_email):
        return {**base, "status": "skipped_service"}

    slug = email_to_slug.get(from_email)
    if not slug:
        # Thread-continuity backstop: if the operator has previously sent on
        # this thread, the sender is engaged regardless of people-brain
        # membership. This covers the gap between "the operator replies to a
        # new contact" and "gmail-sent-mine creates a stub on its next
        # 2-hour cycle". Driver auto-promotes to a real slug.
        if _brian_in_prior_thread_history(thread, operator_emails):
            _name, derived_slug = _derive_name_and_slug(from_email, from_raw)
            return {
                **base,
                "slug": derived_slug,
                "status": "queued",
                "thread_continuity": True,
            }

        # Unknown sender: check recruiter detector before skipping. Cold
        # recruiter inbounds route into drafting (queued_cold_recruiter)
        # so Huckle can evaluate fit against the operator's target_company list
        # and draft a tier-appropriate reply.
        is_rec, rec_conf, rec_signals = is_likely_recruiter(
            from_email=from_email,
            from_header=from_raw,
            subject=subject,
            snippet=snippet,
        )
        if is_rec:
            return {
                **base,
                "status": "queued_cold_recruiter",
                "recruiter_signal_confidence": rec_conf,
                "recruiter_signal_reason": rec_signals.get("reason", ""),
                "recruiter_matched_domain": rec_signals.get("matched_domain", ""),
            }
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

    status = classified.get("status")
    if status == "queued":
        # Pass through only the fields auto-compose reads; stripping
        # transient classifier diagnostics keeps the queue tidy.
        entry = {
            k: classified[k] for k in (
                "thread_id", "slug", "from_email", "from_header",
                "subject", "date", "snippet", "in_reply_to_message_id",
            ) if k in classified
        }
        filtered.append(entry)
    elif status == "queued_cold_recruiter":
        # Cold recruiter: pass through recruiter signals + a `status`
        # field so auto-compose knows to dispatch in cold-inbound mode.
        entry = {
            k: classified[k] for k in (
                "thread_id", "from_email", "from_header",
                "subject", "date", "snippet", "in_reply_to_message_id",
                "recruiter_signal_confidence", "recruiter_signal_reason",
                "recruiter_matched_domain",
            ) if k in classified
        }
        entry["status"] = "queued_cold_recruiter"
        filtered.append(entry)

    return {**base, "queued": filtered}


RECRUITER_CIRCLES = "recruiter, professional-outer"


def auto_promote_thread_continuity(
    classified: dict,
    *,
    operator_emails: set[str],
) -> dict:
    """When classification returned queued + thread_continuity=True,
    write a minimal people/<slug>.md so downstream draft-compose can
    load it. The slug already matches what _derive_name_and_slug
    produced inside the classifier, so promote is idempotent on the
    same thread re-classified across triage cycles.

    Returns status=skipped_not_thread_continuity for any other
    classification, so callers can call unconditionally."""
    if not classified.get("thread_continuity"):
        return {"status": "skipped_not_thread_continuity"}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return promote_to_people_brain(
        from_email=classified.get("from_email", ""),
        from_header=classified.get("from_header", ""),
        circles="professional-outer",
        source="thread_continuity",
        last_interaction=today,
        skip_emails=operator_emails,
        extra_fields={
            "source_thread_id": classified.get("thread_id", ""),
        },
    )


def auto_promote_cold_recruiter(
    classified: dict,
    *,
    operator_emails: set[str],
) -> dict:
    """When a thread classifies as queued_cold_recruiter, write a minimal
    people/<slug>.md so the next message in the same thread (or a future
    inbound from the same sender) is recognized as `queued` via the
    email_to_slug map — no second recruiter-detector pass needed.

    Idempotent: a second call for the same sender returns
    status=already_exists; the operator's manual edits are preserved. Returns
    status=skipped_not_cold_recruiter for any other classification, so
    callers can call this unconditionally inside a result loop without
    branching.
    """
    if classified.get("status") != "queued_cold_recruiter":
        return {"status": "skipped_not_cold_recruiter"}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return promote_to_people_brain(
        from_email=classified.get("from_email", ""),
        from_header=classified.get("from_header", ""),
        circles=RECRUITER_CIRCLES,
        source="triage_recruiter",
        relationship_type="recruiter",
        last_interaction=today,
        skip_emails=operator_emails,
        extra_fields={
            "recruiter_signal_domain": classified.get("recruiter_matched_domain", ""),
            "source_thread_id": classified.get("thread_id", ""),
        },
    )


def build_skipped_samples(
    results: list[dict],
    *,
    max_samples: int = 10,
    subject_cap: int = 100,
) -> list[dict]:
    """Return up to `max_samples` skipped_unknown_sender items so the
    morning brief's Triage Health section can show the operator what got
    silently filtered. Skipped service items are excluded — they're
    pure noise, summarized by the bucket count alone.

    Subjects are truncated to `subject_cap` chars to keep the morning
    Telegram message under the 4096-char limit when several samples
    are listed."""
    out: list[dict] = []
    for r in results:
        if r.get("status") != "skipped_unknown_sender":
            continue
        subject = r.get("subject", "") or ""
        if len(subject) > subject_cap:
            subject = subject[:subject_cap]
        out.append({
            "thread_id": r.get("thread_id", ""),
            "from_email": r.get("from_email", ""),
            "subject": subject,
            "status": r["status"],
        })
        if len(out) >= max_samples:
            break
    return out


def build_queue_from_results(results: list[dict]) -> dict:
    """Apply upsert_thread_in_queue across a list of classified results
    and return the final queue dict. Used by inbox-triage.py's full-scan
    path so it shares persistence semantics with the push-listener
    (single-thread) path — including correct handling of
    queued_cold_recruiter, which the prior inline append loop dropped."""
    queue: dict = {"queued": []}
    for r in results:
        queue = upsert_thread_in_queue(queue, r)
    return queue
