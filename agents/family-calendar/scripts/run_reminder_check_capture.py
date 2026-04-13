#!/usr/bin/env python3
import json
import subprocess
import sys

cmd = ["python3", "/home/node/.openclaw/family-calendar-workspace/scripts/reminder-check.py"]
result = subprocess.run(cmd, capture_output=True, text=True)
print(json.dumps({
    "returncode": result.returncode,
    "stdout": result.stdout,
    "stderr": result.stderr,
}))
if result.returncode != 0:
    sys.exit(result.returncode)
