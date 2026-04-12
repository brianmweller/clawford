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
    """Parse all WhatsApp chat export files."""
    pattern = os.path.join(downloads_dir, "WhatsApp Chat with *.txt")
    files = glob.glob(pattern)

    if not files:
        print("No WhatsApp export files found.", file=sys.stderr)
        return

    print(f"Found {len(files)} WhatsApp exports", file=sys.stderr)

    results = []
    for filepath in sorted(files):
        chat_name, messages = parse_whatsapp_file(filepath)

        # Compute stats
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

        print(f"  {chat_name}: {len(messages)} messages", file=sys.stderr)

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

    count = 0
    # Use iterparse for streaming (4GB file won't fit in memory)
    for event, elem in ET.iterparse(filepath, events=("end",)):
        if elem.tag != "sms":
            continue

        count += 1
        if count % 10000 == 0:
            print(f"  Processed {count} messages...", file=sys.stderr)

        address = elem.get("address", "").strip()
        contact_name = elem.get("contact_name", "").strip()
        body = elem.get("body", "").strip()
        readable_date = elem.get("readable_date", "")
        msg_type = elem.get("type", "1")  # 1=received, 2=sent

        if not address or not body:
            elem.clear()
            continue

        # Normalize phone number as key
        phone_key = re.sub(r"[^\d+]", "", address)
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

        # Parse date for first/last tracking
        date_str = readable_date[:12] if readable_date else None  # "Aug 16, 2016"
        if date_str:
            if not c["first_message"] or readable_date < c["first_message"]:
                c["first_message"] = readable_date
            if not c["last_message"] or readable_date > c["last_message"]:
                c["last_message"] = readable_date

        # Keep sample messages (up to 30 per contact)
        if len(c["messages"]) < 30:
            c["messages"].append({
                "direction": direction,
                "text": body[:500],
                "timestamp": readable_date,
            })

        # Free memory
        elem.clear()

    print(f"  Total: {count} messages, {len(contacts)} contacts", file=sys.stderr)

    # Convert to list format
    results = []
    for phone, c in sorted(contacts.items(), key=lambda x: -x[1]["message_count"]):
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
