#!/usr/bin/env python3
import subprocess, json, sys
p = subprocess.run([
    'python3', '/home/node/.openclaw/family-calendar-workspace/scripts/reminder-check.py'
], capture_output=True, text=True)
print(json.dumps({'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}))
