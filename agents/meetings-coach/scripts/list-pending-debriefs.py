#!/usr/bin/env python3
"""List pending debrief files in cache/."""
import json, os, glob
CACHE = os.path.expanduser("~/.openclaw/meetings-coach-workspace/cache")
files = glob.glob(os.path.join(CACHE, "pending-debrief-*.json"))
debriefs = []
for f in files:
    with open(f) as fh:
        debriefs.append(json.load(fh))
print(json.dumps({"status": "ok", "count": len(debriefs), "debriefs": debriefs}, indent=2))
