"""Gmail API helpers for Huckle Cat's threaded-draft creation and
thread-content fetching.

Uses google-api-python-client. OAuth via agents/shared/google_oauth.py with
gmail.compose scope (caller is responsible for never calling send — our
code only exposes draft creation and readonly fetch).

Production deploy path: auth runs locally (gcal-auth.py), token.json is
SCP'd to the VPS per the fleet's existing Google pattern (memory:
reference_google_oauth).

Pure helpers (build_raw_message, extract_plain_body, thread_to_compose_inputs)
are testable without network or auth; the service wrappers
(build_gmail_service, create_threaded_draft, fetch_inbound_message_id,
fetch_thread) are thin and exercise the real Gmail API when invoked.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import parseaddr


# Never include gmail.send, gmail.modify, gmail.readonly here. The caller
# who wants those scopes combines them at their own layer (e.g. the
# connector agent's gcal-auth.py bundles readonly + compose).
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"


def build_raw_message(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to_message_id: str | None = None,
) -> str:
    """Build a base64url-encoded RFC822 message for the Gmail drafts API.

    When in_reply_to_message_id is provided, sets In-Reply-To and
    References headers so Gmail threads the draft correctly alongside
    the explicit threadId passed to drafts().create(). Either alone
    usually works, but providing both is the robust form.
    """
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if in_reply_to_message_id:
        msg["In-Reply-To"] = in_reply_to_message_id
        msg["References"] = in_reply_to_message_id
    # cte="8bit": Python's default policy encodes as quoted-printable with
    # 76-column soft wraps (`=\n`). Gmail's compose renderer treats those
    # as hard line breaks, so a dehardwrapped paragraph reappears chopped
    # at ~65-70 chars. 8BITMIME is universally supported (incl. Gmail).
    msg.set_content(body, cte="8bit")
    return base64.urlsafe_b64encode(bytes(msg)).decode("ascii")


def build_gmail_service(token_path: str, creds_path: str, scopes: list[str] | None = None):
    """Return a Gmail API service client from existing token/creds.

    Scopes default to [COMPOSE_SCOPE]; caller can pass a wider set (e.g.
    readonly + compose) if the same token needs to serve multiple roles.
    """
    from googleapiclient.discovery import build
    from agents.shared.google_oauth import get_credentials

    scopes = scopes or [COMPOSE_SCOPE]
    creds = get_credentials(creds_path, token_path, scopes)
    return build("gmail", "v1", credentials=creds)


def create_threaded_draft(
    service,
    *,
    thread_id: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to_message_id: str | None = None,
) -> dict:
    """Create a Gmail draft threaded into an existing thread.

    Returns the draft resource: {"id": "...", "message": {...}}.
    """
    raw = build_raw_message(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        in_reply_to_message_id=in_reply_to_message_id,
    )
    body_dict = {
        "message": {
            "raw": raw,
            "threadId": thread_id,
        }
    }
    return service.users().drafts().create(userId="me", body=body_dict).execute()


def fetch_thread(service, thread_id: str) -> dict:
    """Fetch a Gmail thread in format=full (bodies + headers included)."""
    return service.users().threads().get(userId="me", id=thread_id, format="full").execute()


def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _header_value(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "") or ""
    return ""


def extract_plain_body(message: dict) -> str:
    """Walk a Gmail message payload and return the plain-text body.
    Prefers text/plain parts over text/html for multipart messages."""
    payload = message.get("payload", {})

    # Simple case: body directly on payload
    body = payload.get("body", {}) or {}
    if body.get("data"):
        if payload.get("mimeType", "").startswith("text/plain") or not payload.get("parts"):
            return _decode_b64url(body["data"])

    # Multipart: prefer text/plain at any depth
    def _walk(parts):
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = (part.get("body") or {}).get("data")
                if data:
                    return _decode_b64url(data)
            sub = part.get("parts") or []
            if sub:
                found = _walk(sub)
                if found:
                    return found
        return None

    result = _walk(payload.get("parts") or [])
    return result or ""


def thread_to_compose_inputs(thread: dict, operator_emails: set[str]) -> tuple[dict, list[dict]]:
    """Convert a Gmail threads.get(format=full) response into the
    (inbound, history) shape draft-compose.py consumes from its fixture
    JSON files.

    The "latest inbound" is the most recent message whose From is NOT
    one of operator_emails. History is every message BEFORE that one,
    in chronological order.

    Raises ValueError if the thread is empty or has no inbound message.
    """
    messages = thread.get("messages") or []
    if not messages:
        raise ValueError("thread has no messages")

    brian_lower = {a.lower() for a in operator_emails}

    # Find latest inbound message (walking from the end)
    latest_inbound = None
    latest_index = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        from_raw = _header_value(m, "From")
        _, addr = parseaddr(from_raw or "")
        from_email = (addr or "").lower()
        if from_email and "@" in from_email and from_email not in brian_lower:
            latest_inbound = m
            latest_index = i
            break

    if latest_inbound is None:
        raise ValueError("thread has no inbound message (only the operator's own)")

    from_raw = _header_value(latest_inbound, "From")
    from_name, from_email = parseaddr(from_raw or "")

    def _split_addrs(header: str) -> list[str]:
        """Split a To/Cc header into individual email addresses."""
        if not header:
            return []
        return [
            parseaddr(part)[1]
            for part in header.split(",")
            if parseaddr(part)[1]
        ]

    inbound = {
        "from_name": from_name or "",
        "from_email": from_email or "",
        "to": _split_addrs(_header_value(latest_inbound, "To")),
        "cc": _split_addrs(_header_value(latest_inbound, "Cc")),
        "subject": _header_value(latest_inbound, "Subject"),
        "received_at": _header_value(latest_inbound, "Date"),
        "body": extract_plain_body(latest_inbound),
    }

    history: list[dict] = []
    for m in messages[:latest_index]:
        from_raw = _header_value(m, "From")
        name, addr = parseaddr(from_raw or "")
        addr_lower = (addr or "").lower()
        who = "the operator" if addr_lower in brian_lower else (name or addr or "?")
        history.append({
            "from": who,
            "date": _header_value(m, "Date"),
            "body": extract_plain_body(m),
        })

    return inbound, history


def fetch_inbound_message_id(service, thread_id: str) -> str | None:
    """Pull the RFC822 Message-ID header of the most recent inbound
    message in the thread. Used by create_threaded_draft as the
    In-Reply-To value."""
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["Message-ID", "From"],
    ).execute()
    messages = thread.get("messages", [])
    if not messages:
        return None
    for m in reversed(messages):
        headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
        msg_id = headers.get("Message-ID") or headers.get("Message-Id")
        if msg_id:
            return msg_id
    return None
