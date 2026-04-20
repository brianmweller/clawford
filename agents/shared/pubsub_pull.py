"""pubsub_pull — thin wrappers around the Google Cloud Pub/Sub REST API
(googleapiclient discovery), scoped to the pull/ack flow the Huckle
Gmail listener needs.

Why REST (googleapiclient) instead of google-cloud-pubsub gRPC:

  * The fleet already has google-api-python-client installed for Gmail/
    Calendar/Tasks. Adding google-cloud-pubsub pulls in grpcio + extra
    transitive deps for a benefit (streaming pull) we don't need — our
    volume is <1 msg/min on average.
  * User-OAuth credentials (the operator's token) work transparently through
    googleapiclient; the gRPC library wants either a service account
    or an ADC setup.
  * The REST pull endpoint accepts returnImmediately=False for
    long-poll semantics — same tail latency as streaming pull for our
    throughput.

Build a service with:
    from googleapiclient.discovery import build
    service = build("pubsub", "v1", credentials=creds)

Required OAuth scope: https://www.googleapis.com/auth/pubsub
(already added to agents/shared/google_oauth.py's PUBSUB_SCOPE).
Gmail's users.watch() requires NO pubsub scope; only the consumer side
does.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GmailPushNotification:
    """The payload Gmail writes to its Pub/Sub topic on every label
    change (INBOX-filtered in our setup). historyId is the cursor the
    listener passes to users.history.list() to retrieve the delta."""
    email_address: str
    history_id: int


@dataclass(frozen=True)
class PulledMessage:
    """One message from pull_messages(). ack_id must be passed back to
    acknowledge_messages() or Pub/Sub will redeliver after the
    subscription's ackDeadline.

    gmail_push is None if the Pub/Sub payload couldn't be parsed as a
    Gmail push notification. Callers should ack these anyway (they're
    unrecoverable) and log the failure."""
    ack_id: str
    message_id: str
    publish_time: str
    gmail_push: GmailPushNotification | None


def decode_gmail_push(data_b64: str) -> GmailPushNotification | None:
    """Decode a base64-encoded Gmail push notification. Returns None on
    any decode / parse / schema failure so the caller can treat all
    unparseable payloads uniformly."""
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    email = payload.get("emailAddress")
    history = payload.get("historyId")
    if email is None or history is None:
        return None
    try:
        history_id = int(history)
    except (TypeError, ValueError):
        return None
    return GmailPushNotification(email_address=str(email), history_id=history_id)


def parse_pull_response(response: dict) -> list[PulledMessage]:
    """Normalize a pubsub.subscriptions.pull() response into a list of
    PulledMessage. Pure helper — no network."""
    out: list[PulledMessage] = []
    for row in response.get("receivedMessages") or []:
        ack_id = row.get("ackId", "")
        msg = row.get("message") or {}
        data = msg.get("data", "")
        push = decode_gmail_push(data) if data else None
        out.append(PulledMessage(
            ack_id=ack_id,
            message_id=msg.get("messageId", ""),
            publish_time=msg.get("publishTime", ""),
            gmail_push=push,
        ))
    return out


def pull_messages(
    service: Any,
    *,
    subscription_path: str,
    max_messages: int = 10,
) -> list[PulledMessage]:
    """Long-poll pull up to max_messages from the subscription.

    subscription_path is the fully-qualified REST name, e.g.
    "projects/<project>/subscriptions/<name>".

    returnImmediately=False enables Pub/Sub's long-poll (server holds
    the connection open for up to ~90s waiting for a message before
    returning empty). Much cheaper + lower latency than a tight poll
    loop with returnImmediately=True.
    """
    body = {
        "maxMessages": max_messages,
        "returnImmediately": False,
    }
    response = (
        service.projects().subscriptions().pull(
            subscription=subscription_path, body=body,
        ).execute()
    )
    return parse_pull_response(response)


def acknowledge_messages(
    service: Any,
    *,
    subscription_path: str,
    ack_ids: list[str],
) -> None:
    """Ack a batch of messages. No-op if ack_ids is empty so the caller
    can pass the result of a list comprehension without guarding."""
    if not ack_ids:
        return
    body = {"ackIds": ack_ids}
    service.projects().subscriptions().acknowledge(
        subscription=subscription_path, body=body,
    ).execute()
