from pathlib import Path
from datetime import datetime, timezone, timedelta

workspace = Path.home() / '.openclaw' / 'meetings-coach-workspace'
status_path = Path.home() / 'Dropbox' / 'openclaw-backup' / 'agents' / 'meetings-coach.status.md'
cache_dir = workspace / 'cache'

# Verify required files exist
for required in [workspace / 'meeting-config.json', workspace / 'sent-alerts.json']:
    if not required.exists():
        raise SystemExit(f'missing required file: {required}')

# Prune old prep files
cutoff = datetime(2026, 4, 9, 12, 8, 0, tzinfo=timezone.utc) - timedelta(days=14)
for path in cache_dir.glob('prep-*'):
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if mtime < cutoff:
        path.unlink()

# Update heartbeat
text = status_path.read_text()
new_line = '- **last_heartbeat:** 2026-04-09 12:08 UTC'
lines = text.splitlines()
for i, line in enumerate(lines):
    if line.startswith('- **last_heartbeat:**'):
        lines[i] = new_line
        break
else:
    lines.append(new_line)
status_path.write_text('\n'.join(lines) + ('\n' if text.endswith('\n') or not lines[-1].endswith('\n') else ''))
