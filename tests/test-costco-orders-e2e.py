#!/usr/bin/env python3
"""End-to-end tests for costco-orders.py on VPS.

Requires: valid costco-tokens.json at ~/.openclaw/shopping-workspace/.
Requires: PROXY_URL in env (or COSTCO_EMAIL + COSTCO_PASSWORD for auth).
Run on VPS: docker compose exec -T openclaw-gateway python3 /path/to/test-costco-orders-e2e.py
"""

import json
import os
import subprocess
import sys

SCRIPT = os.path.expanduser("~/.openclaw/shopping-workspace/scripts/costco-orders.py")
TOKEN_FILE = os.path.expanduser("~/.openclaw/shopping-workspace/costco-tokens.json")
PYTHON = sys.executable

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}" + (f" -- {detail}" if detail else ""))
        failed += 1


def run_script(args, timeout=60):
    result = subprocess.run(
        [PYTHON, SCRIPT] + args,
        capture_output=True, text=True, timeout=timeout,
    )
    return result


def test_recent():
    """T1-T2: Recent orders via GraphQL."""
    print("\nT1-T2: Recent Costco orders (--recent --days 60)")

    # Clear cache
    cache_dir = os.path.expanduser("~/.openclaw/shopping-workspace/cache")
    for f in os.listdir(cache_dir) if os.path.exists(cache_dir) else []:
        if f.startswith("costco-"):
            os.remove(os.path.join(cache_dir, f))

    r = run_script(["--recent", "--days", "60"])
    check("T1: exit code 0", r.returncode == 0, f"exit {r.returncode}: {r.stderr[:200]}")

    if r.returncode != 0:
        return

    data = json.loads(r.stdout)
    check("T1: has query field", data.get("query") == "recent")
    check("T1: has count", isinstance(data.get("count"), int))
    check("T1: has orders array", isinstance(data.get("orders"), list))
    check("T1: count > 0", data["count"] > 0, f"count={data['count']}")

    if data["count"] > 0:
        order = data["orders"][0]
        check("T2: order has orderNumber", "orderNumber" in order)
        check("T2: order has orderPlacedDate", "orderPlacedDate" in order)
        check("T2: order has orderTotal", "orderTotal" in order)
        check("T2: order has status", "status" in order)
        check("T2: order has orderLineItems", "orderLineItems" in order)

        if order.get("orderLineItems"):
            item = order["orderLineItems"][0]
            check("T2: item has itemDescription", "itemDescription" in item)
            check("T2: item has deliveryDate", "deliveryDate" in item)


def test_search():
    """T3-T4: Search functionality."""
    print("\nT3: Search for known item")
    r = run_script(["--search", "Huggies"])
    check("T3: exit code 0", r.returncode == 0, f"exit {r.returncode}: {r.stderr[:200]}")

    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("T3: has search_term", data.get("search_term") == "Huggies")
        check("T3: found matches", data.get("count", 0) > 0,
              f"count={data.get('count')}")

    print("\nT4: Search for nonexistent item")
    r = run_script(["--search", "xyznonexistentproduct123"])
    check("T4: exit code 0", r.returncode == 0, f"exit {r.returncode}: {r.stderr[:200]}")

    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("T4: count is 0", data.get("count") == 0)


if __name__ == "__main__":
    print("=" * 50)
    print("Hilda Hippo -- Costco Orders E2E Tests")
    print("=" * 50)

    if not os.path.exists(SCRIPT):
        print(f"ERROR: Script not found at {SCRIPT}")
        sys.exit(1)

    if not os.path.exists(TOKEN_FILE):
        print(f"ERROR: Token file not found at {TOKEN_FILE}")
        print("Run costco-capture-session.py locally and SCP the token to VPS")
        sys.exit(1)

    # Check token freshness
    with open(TOKEN_FILE) as f:
        tokens = json.load(f)
    if not tokens.get("id_token"):
        print("ERROR: No id_token in token file")
        sys.exit(1)

    test_recent()
    test_search()

    print("\n" + "=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 50)

    sys.exit(1 if failed else 0)
