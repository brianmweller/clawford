"""Gmail API helpers for Huckle Cat's threaded-draft creation.

Uses google-api-python-client. OAuth via agents/shared/google_oauth.py with
gmail.compose scope (caller is responsible for never calling send — our
code only exposes draft creation).

Production deploy path: auth runs locally (gcal-auth.py), token.json is
SCP'd to the VPS per the fleet's existing Google pattern (memory:
reference_google_oauth).

Pure helper (build_raw_message) is testable without network or auth; the
service wrappers (build_gmail_service, create_threaded_draft) are thin
and exercise the real Gmail API when invoked.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage


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
    msg.set_content(body)
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
