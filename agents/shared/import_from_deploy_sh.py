#!/usr/bin/env python3
"""
import_from_deploy_sh.py — One-shot importer: parse an agent's deploy.sh and
emit manifest.json.

Used during migration from shell-based deploys to the unified deploy.py
pipeline. Parses `oc cron add` blocks via shlex + quote-aware block joining
(same logic used ad-hoc on 2026-04-11 to patch all 4 non-fix-it agents),
then augments with per-agent workspace / telegram metadata.

Usage:
    python3 import_from_deploy_sh.py <agent_id>

Writes to agents/<agent_id>/manifest.json. Overwrites if present — review
the diff before committing.
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = REPO_ROOT / "agents"


# Per-agent defaults that can't be inferred from deploy.sh alone. Mirror
# what the shell scripts hardcode in their top-of-file block.
AGENT_DEFAULTS = {
    "shopping": {
        "display_name": "Hilda Hippo",
        "telegram": {"account": "shopping", "bot_token_env": "SHOPPING_BOT_TOKEN"},
    },
    "family-calendar": {
        "display_name": "Mistress Mouse",
        "telegram": {"account": "familycal", "bot_token_env": "FAMILYCAL_BOT_TOKEN"},
    },
    "meetings-coach": {
        "display_name": "Sergeant Murphy",
        "telegram": {"account": "murphy", "bot_token_env": "MURPHY_BOT_TOKEN"},
    },
    "news-digest": {
        "display_name": "Lowly Worm",
        "telegram": {"account": "newsdigest", "bot_token_env": "NEWSDIGEST_BOT_TOKEN"},
    },
    "connector": {
        "display_name": "Huckle Cat",
        "telegram": {"account": "huckle", "bot_token_env": "CONNECTOR_BOT_TOKEN"},
    },
}


def load_vars(text: str) -> dict[str, str]:
    vars_ = {"TELEGRAM_CHAT_ID": "$TELEGRAM_CHAT_ID", "HOME": "~"}
    # VAR="value"
    for m in re.finditer(r'^([A-Z_][A-Z0-9_]*)="([^"\n]*)"', text, re.M):
        vars_.setdefault(m.group(1), m.group(2))
    # VAR='multi-line'
    for m in re.finditer(
        r"^([A-Z_][A-Z0-9_]*)='((?:[^']|(?<=\\)')*)'",
        text, re.M | re.S,
    ):
        vars_[m.group(1)] = m.group(2)
    return vars_


def expand_vars(s: str, vars_: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return vars_.get(name, m.group(0))
    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)", repl, s)


def _in_quotes(s: str) -> bool:
    d = sq = 0
    esc = False
    for ch in s:
        if esc:
            esc = False; continue
        if ch == "\\":
            esc = True; continue
        if ch == '"' and sq % 2 == 0:
            d += 1
        elif ch == "'" and d % 2 == 0:
            sq += 1
    return (d % 2 == 1) or (sq % 2 == 1)


def _trailing_backslash(s: str) -> bool:
    stripped = s.rstrip()
    if not stripped:
        return False
    count = 0
    k = len(stripped) - 1
    while k >= 0 and stripped[k] == "\\":
        count += 1
        k -= 1
    return count % 2 == 1


def find_cron_blocks(text: str):
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("oc cron add"):
            block_parts = [line]
            while True:
                joined = "\n".join(block_parts)
                still_quoted = _in_quotes(joined)
                last = block_parts[-1]
                if not still_quoted and not _trailing_backslash(last):
                    break
                i += 1
                if i >= len(lines):
                    break
                block_parts.append(lines[i])
            yield re.sub(r"\\\n", " ", "\n".join(block_parts))
        i += 1


def parse_cron_block(block: str, vars_: dict[str, str]) -> dict | None:
    try:
        tokens = shlex.split(re.sub(r"\\\s*\n", " ", block), posix=True)
    except ValueError as e:
        print(f"  shlex error: {e}", file=sys.stderr)
        return None
    if tokens[:3] != ["oc", "cron", "add"]:
        return None
    tokens = tokens[3:]
    out = {
        "name": "", "cron": "", "message": "",
        "announce": False, "no_deliver": False,
    }
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--name" and i + 1 < len(tokens):
            out["name"] = expand_vars(tokens[i + 1], vars_); i += 2
        elif t == "--cron" and i + 1 < len(tokens):
            out["cron"] = expand_vars(tokens[i + 1], vars_); i += 2
        elif t == "--message" and i + 1 < len(tokens):
            out["message"] = expand_vars(tokens[i + 1], vars_); i += 2
        elif t == "--announce":
            out["announce"] = True; i += 1
        elif t == "--no-deliver":
            out["no_deliver"] = True; i += 1
        else:
            i += 1
    return out if out["name"] else None


# ────────────────────────────────────────────────────────────────────────
# File / script / state inference
# ────────────────────────────────────────────────────────────────────────


def find_config_files(deploy_text: str, agent_dir: Path) -> list[dict]:
    """Scan the `for file in ... ; do cp /tmp/$file ...` block for listed md files."""
    m = re.search(
        r"for file in ([^;]+); do\s+if \[ -f \"/tmp/\$file\"",
        deploy_text,
    )
    if not m:
        return []
    names = m.group(1).split()
    immutables = set(re.findall(r'chattr \+i "\$WORKSPACE/([^"]+)"', deploy_text))
    result = []
    for name in names:
        entry = {"src": name}
        if name in immutables:
            entry["immutable"] = True
        result.append(entry)
    return result


def find_scripts(deploy_text: str) -> list[str]:
    m = re.search(
        r"for script in ([^;]+); do\s+if \[ -f \"/tmp/scripts/\$script\"",
        deploy_text,
    )
    if not m:
        return []
    return [f"scripts/{s}" for s in m.group(1).split()]


def find_state_files(deploy_text: str) -> list[dict]:
    """Look for `cat > "$WORKSPACE/foo.json" << 'EOF' ... EOF` heredocs."""
    state_files: list[dict] = []
    pattern = re.compile(
        r'cat > "\$WORKSPACE/([^"]+)" << \'EOF\'\n(.*?)\nEOF',
        re.S,
    )
    for m in pattern.finditer(deploy_text):
        path = m.group(1)
        body = m.group(2)
        try:
            parsed = json.loads(body)
            state_files.append({"path": path, "seed_if_absent": parsed})
        except json.JSONDecodeError:
            state_files.append({"path": path, "seed_if_absent": body})
    return state_files


def find_approvals(deploy_text: str) -> list[str]:
    patterns = re.findall(
        r'oc approvals allowlist add --agent \S+ "([^"]+)"',
        deploy_text,
    )
    return patterns or ["/usr/bin/*", "/bin/*", "/usr/local/bin/*"]


# ────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: import_from_deploy_sh.py <agent_id>", file=sys.stderr)
        return 2

    agent_id = sys.argv[1]
    agent_dir = AGENTS_DIR / agent_id
    deploy_sh = agent_dir / "deploy.sh"
    if not deploy_sh.exists():
        print(f"No deploy.sh at {deploy_sh}", file=sys.stderr)
        return 2

    text = deploy_sh.read_text(encoding="utf-8")
    vars_ = load_vars(text)

    crons = []
    for block in find_cron_blocks(text):
        parsed = parse_cron_block(block, vars_)
        if parsed:
            crons.append(parsed)

    defaults = AGENT_DEFAULTS.get(agent_id, {})
    manifest = {
        "agent_id": agent_id,
        "display_name": defaults.get("display_name", agent_id),
        "workspace": f"~/.clawford/{agent_id}-workspace",
        "telegram": defaults.get("telegram", {"account": agent_id, "bot_token_env": ""}),
        "config_files": find_config_files(text, agent_dir),
        "scripts": find_scripts(text),
        "state_files": find_state_files(text),
        "approvals": {"allowlist": find_approvals(text)},
        "crons": crons,
    }

    out_path = agent_dir / "manifest.json"
    out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {out_path}")
    print(f"  {len(manifest['config_files'])} config files")
    print(f"  {len(manifest['scripts'])} scripts")
    print(f"  {len(manifest['state_files'])} state files")
    print(f"  {len(manifest['crons'])} crons")
    return 0


if __name__ == "__main__":
    sys.exit(main())
