#!/usr/bin/env python3
"""
parse-phone-exports.py — Parse WhatsApp .txt exports + SMS Backup & Restore .xml

Reads phone exports and converts to the standard mined-*.json format
for the aggregator.

Usage:
  python3 parse-phone-exports.py --downloads "C:/Users/Sam/Downloads"

Outputs:
  cache/mined-whatsapp.json   (from WhatsApp Chat with *.txt files)
  cache/mined-messages.json   (from sms-*.xml file)
"""

import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR, load_config

CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── WhatsApp .txt parser ─────────────────────────────────────

# Format: "M/D/YY, HH:MM - Author: message"
WA_MSG_RE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2})\s*-\s*(.*?):\s(.+)"
)
WA_SYSTEM_RE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2})\s*-\s(.+)"
)


def parse_whatsapp_file(filepath):
    """Parse a single WhatsApp chat export .txt file."""
    messages = []
    # Get chat name from filename: "WhatsApp Chat with NAME.txt"
    basename = os.path.basename(filepath)
    chat_name = basename.replace("WhatsApp Chat with ", "").replace(".txt", "")

    with open(filepath, "r", encoding="utf-8") as f:
        current_msg = None
        for line in f:
            line = line.rstrip("\n")
            match = WA_MSG_RE.match(line)
            if match:
                # Save previous message
                if current_msg:
                    messages.append(current_msg)

                date_str = match.group(1)
                time_str = match.group(2)
                author = match.group(3).strip()
                text = match.group(4).strip()

                # Skip media placeholders
                if text in ("<Media omitted>", "image omitted", "video omitted",
                            "audio omitted", "sticker omitted", "GIF omitted"):
                    current_msg = None
                    continue

                is_brian = author.lower() in ("Sam", "Sam Smith", "you")

                current_msg = {
                    "direction": "outbound" if is_brian else "inbound",
                    "author": author,
                    "text": text[:500],
                    "timestamp": f"{date_str} {time_str}",
                }
            elif current_msg and line.startswith("    "):
                # Continuation of previous message (indented)
                current_msg["text"] += "\n" + line.strip()
                current_msg["text"] = current_msg["text"][:500]
            elif WA_SYSTEM_RE.match(line):
                # System message (encryption notice, etc.) — skip
                if current_msg:
                    messages.append(current_msg)
                current_msg = None

        if current_msg:
            messages.append(current_msg)

    return chat_name, messages


def parse_all_whatsapp(downloads_dir):
    """Parse all WhatsApp chat export files.

    1:1 chats: one contact per file, using the chat filename as the name.
    Group chats: detected when >1 distinct non-Sam author posts. Split
    messages per author and emit one contact per author. The group chat
    aggregate is NOT emitted (no spurious "FamilyGroup" contact).
    """
    pattern = os.path.join(downloads_dir, "WhatsApp Chat with *.txt")
    files = glob.glob(pattern)

    if not files:
        print("No WhatsApp export files found.", file=sys.stderr)
        return

    print(f"Found {len(files)} WhatsApp exports", file=sys.stderr)

    results = []
    for filepath in sorted(files):
        chat_name, messages = parse_whatsapp_file(filepath)

        # Count distinct non-Sam authors
        non_brian_authors = {
            m["author"] for m in messages
            if m.get("direction") == "inbound" and m.get("author")
        }

        if len(non_brian_authors) <= 1:
            # 1:1 chat — emit as single contact (use chat_name, not author)
            first_ts = messages[0]["timestamp"] if messages else ""
            last_ts = messages[-1]["timestamp"] if messages else ""
            results.append({
                "name": chat_name,
                "platform": "whatsapp",
                "messages": messages,
                "message_count": len(messages),
                "first_message": first_ts,
                "last_message": last_ts,
            })
            print(f"  {chat_name}: {len(messages)} messages (1:1)", file=sys.stderr)
        else:
            # Group chat — split per author. Sam's messages are replicated
            # to each author's contact (as the "outbound to them" record).
            brian_msgs = [m for m in messages if m.get("direction") == "outbound"]
            print(f"  {chat_name}: GROUP ({len(non_brian_authors)} authors, {len(messages)} msgs) — splitting", file=sys.stderr)
            for author in sorted(non_brian_authors):
                author_inbound = [m for m in messages if m.get("author") == author]
                # Merge inbound from this author + Sam's outbound in temporal order
                all_msgs = sorted(
                    author_inbound + brian_msgs,
                    key=lambda m: m.get("timestamp", ""),
                )
                if not all_msgs:
                    continue
                results.append({
                    "name": author,
                    "platform": "whatsapp",
                    "messages": all_msgs,
                    "message_count": len(all_msgs),
                    "first_message": all_msgs[0]["timestamp"],
                    "last_message": all_msgs[-1]["timestamp"],
                    "from_group": chat_name,
                })
                print(f"    → {author}: {len(all_msgs)} msgs", file=sys.stderr)

    total_msgs = sum(r["message_count"] for r in results)
    output = {
        "status": "ok",
        "source": "whatsapp",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "conversations_scanned": len(results),
        "contacts_found": len(results),
        "total_messages": total_msgs,
        "contacts": results,
    }

    out_path = CACHE_DIR / "mined-whatsapp.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nWhatsApp: {total_msgs} messages from {len(results)} chats → {out_path}", file=sys.stderr)


# ── SMS Backup & Restore .xml parser ─────────────────────────

SAMPLE_CAP = 50  # Per-contact sample cap (most-recent)


def parse_sms_xml(filepath):
    """Parse SMS Backup & Restore XML using streaming to handle large files."""
    print(f"Parsing SMS XML (this may take a moment for large files)...", file=sys.stderr)

    contacts = defaultdict(lambda: {
        "name": "",
        "phone": "",
        "platform": "sms",
        "messages": [],
        "message_count": 0,
        "inbound_count": 0,
        "outbound_count": 0,
        "first_message": None,
        "last_message": None,
    })

    def normalize_phone(raw):
        """Normalize phone to digits only, strip leading +1 for US numbers."""
        digits = re.sub(r"[^\d]", "", raw)
        if digits.startswith("1") and len(digits) == 11:
            digits = digits[1:]  # Strip US country code
        return digits

    sms_count = 0
    mms_count = 0

    # Track current MMS context for part processing
    current_mms = None

    # Use iterparse for streaming (4GB file won't fit in memory)
    for event, elem in ET.iterparse(filepath, events=("start", "end")):
        # Capture MMS attributes on open tag (before children are processed)
        if event == "start" and elem.tag == "mms":
            current_mms = {
                "address": elem.get("address", ""),
                "contact_name": elem.get("contact_name", ""),
                "readable_date": elem.get("readable_date", ""),
                "msg_box": elem.get("msg_box", "1"),
                "body": "",
            }
            continue

        # Capture text/plain part body
        if event == "end" and elem.tag == "part" and current_mms is not None:
            if elem.get("ct") == "text/plain":
                text = (elem.get("text") or "").strip()
                if text and not current_mms["body"]:
                    current_mms["body"] = text
            elem.clear()
            continue

        if event != "end":
            continue

        if elem.tag == "sms":
            sms_count += 1
            if (sms_count + mms_count) % 10000 == 0:
                print(f"  Processed {sms_count + mms_count} messages ({sms_count} SMS, {mms_count} MMS)...", file=sys.stderr)

            address = elem.get("address", "").strip()
            contact_name = elem.get("contact_name", "").strip()
            body = elem.get("body", "").strip()
            readable_date = elem.get("readable_date", "")
            msg_type = elem.get("type", "1")  # 1=received, 2=sent

            if not address or not body:
                elem.clear()
                continue

            phone_key = normalize_phone(address)
            if not phone_key:
                elem.clear()
                continue

            c = contacts[phone_key]
            c["phone"] = address
            if contact_name and contact_name not in ("(Unknown)", "null", ""):
                c["name"] = contact_name

            direction = "outbound" if msg_type == "2" else "inbound"
            c["message_count"] += 1
            if direction == "outbound":
                c["outbound_count"] += 1
            else:
                c["inbound_count"] += 1

            if readable_date:
                if not c["first_message"] or readable_date < c["first_message"]:
                    c["first_message"] = readable_date
                if not c["last_message"] or readable_date > c["last_message"]:
                    c["last_message"] = readable_date

            c["messages"].append({
                "direction": direction,
                "text": body[:500],
                "timestamp": readable_date,
            })

            elem.clear()

        elif elem.tag == "mms" and current_mms is not None:
            mms_count += 1
            if (sms_count + mms_count) % 10000 == 0:
                print(f"  Processed {sms_count + mms_count} messages ({sms_count} SMS, {mms_count} MMS)...", file=sys.stderr)

            raw_address = current_mms["address"].strip()
            contact_name = current_mms["contact_name"].strip()
            readable_date = current_mms["readable_date"]
            msg_box = current_mms["msg_box"]
            body = current_mms["body"]

            current_mms = None  # Reset

            if not raw_address or not body:
                elem.clear()
                continue

            # For group MMS, address has multiple numbers: +1234~+5678~+9012
            # Use the first non-Sam number, or the contact_name
            addresses = [a.strip() for a in raw_address.split("~") if a.strip()]
            direction = "outbound" if msg_box == "2" else "inbound"

            # Pick the primary contact (for group chats, use contact_name)
            for addr in addresses:
                phone_key = normalize_phone(addr)
                if not phone_key:
                    continue

                c = contacts[phone_key]
                c["phone"] = addr
                if contact_name and contact_name not in ("(Unknown)", "null", ""):
                    # For group MMS, contact_name may be "Name1, Name2, Name3"
                    # Use the full group name as the contact name
                    c["name"] = contact_name

                c["message_count"] += 1
                if direction == "outbound":
                    c["outbound_count"] += 1
                else:
                    c["inbound_count"] += 1

                if readable_date:
                    if not c["first_message"] or readable_date < c["first_message"]:
                        c["first_message"] = readable_date
                    if not c["last_message"] or readable_date > c["last_message"]:
                        c["last_message"] = readable_date

                c["messages"].append({
                    "direction": direction,
                    "text": body[:500],
                    "timestamp": readable_date,
                })

                break  # Only attribute to one contact per MMS

            elem.clear()

        else:
            elem.clear()

    print(f"  Total: {sms_count} SMS + {mms_count} MMS = {sms_count + mms_count} messages, {len(contacts)} contacts", file=sys.stderr)

    # Convert to list format. For each contact, sort messages by parsed
    # timestamp descending and keep the SAMPLE_CAP most recent.
    from datetime import datetime as _dt
    def _ts_key(msg):
        ts = msg.get("timestamp", "")
        if not ts:
            return _dt.min
        # SMS Backup format: "Nov 22, 2017 09:42:26" or similar
        for fmt in ("%b %d, %Y %H:%M:%S", "%b %d, %Y %I:%M:%S %p",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return _dt.strptime(ts, fmt)
            except ValueError:
                continue
        return _dt.min

    results = []
    for phone, c in sorted(contacts.items(), key=lambda x: -x[1]["message_count"]):
        msgs = c["messages"]
        msgs_sorted = sorted(msgs, key=_ts_key, reverse=True)
        c["messages"] = msgs_sorted[:SAMPLE_CAP]
        results.append(c)

    total_msgs = sum(r["message_count"] for r in results)
    output = {
        "status": "ok",
        "source": "messages",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "conversations_scanned": len(results),
        "contacts_found": len(results),
        "total_messages": total_msgs,
        "contacts": results,
    }

    out_path = CACHE_DIR / "mined-messages.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSMS: {total_msgs} messages from {len(results)} contacts → {out_path}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────

def main():
    downloads_dir = "C:/Users/Sam/Downloads"
    for i, arg in enumerate(sys.argv):
        if arg == "--downloads" and i + 1 < len(sys.argv):
            downloads_dir = sys.argv[i + 1]

    # Parse WhatsApp exports
    parse_all_whatsapp(downloads_dir)

    # Parse SMS XML
    sms_files = glob.glob(os.path.join(downloads_dir, "sms-*.xml"))
    if sms_files:
        # Use the most recent one
        sms_file = max(sms_files, key=os.path.getmtime)
        parse_sms_xml(sms_file)
    else:
        print("No SMS XML file found in downloads.", file=sys.stderr)

    print("\nDone! Both outputs in cache/", file=sys.stderr)


if __name__ == "__main__":
    main()
