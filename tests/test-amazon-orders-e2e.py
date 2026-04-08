#!/usr/bin/env python3
"""End-to-end tests for amazon-orders.py on VPS.

Requires: AMAZON_USERNAME, AMAZON_PASSWORD, PROXY_URL in env.
Requires: saved cookies at ~/.openclaw/shopping-workspace/amazon-cookies.json.
Run on VPS: docker compose exec -T openclaw-gateway python3 /path/to/test-amazon-orders-e2e.py
"""

import json
import os
import subprocess
import sys
import time

SCRIPT = os.path.expanduser("~/.openclaw/shopping-workspace/scripts/amazon-orders.py")
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


def run_script(args, timeout=120):
    result = subprocess.run(
        [PYTHON, SCRIPT] + args,
        capture_output=True, text=True, timeout=timeout,
    )
    return result


def test_recent():
    """T1-T2: Recent orders."""
    print("\nT1-T2: Recent orders (--recent --days 30)")

    # Clear cache first
    cache_dir = os.path.expanduser("~/.openclaw/shopping-workspace/cache")
    for f in os.listdir(cache_dir) if os.path.exists(cache_dir) else []:
        if f.startswith("amazon-"):
            os.remove(os.path.join(cache_dir, f))

    r = run_script(["--recent", "--days", "30"])
    check("T1: exit code 0", r.returncode == 0, f"exit {r.returncode}: {r.stderr[:200]}")

    if r.returncode != 0:
        return

    data = json.loads(r.stdout)
    check("T1: has query field", data.get("query") == "recent")
    check("T1: has count", isinstance(data.get("count"), int))
    check("T1: has orders array", isinstance(data.get("orders"), list))
    check("T1: has fetched_at", data.get("fetched_at") is not None)

    if data["count"] > 0:
        order = data["orders"][0]
        check("T2: order has order_number", "order_number" in order)
        check("T2: order has order_date", "order_date" in order)
        check("T2: order has total", "total" in order)
        check("T2: order has status", "status" in order)
        check("T2: order has items", "items" in order and isinstance(order["items"], list))
        check("T2: order_number format", len(order["order_number"]) > 10)
    else:
        print("  SKIP: No orders to validate structure (count=0)")


def test_search():
    """T3-T4: Search functionality."""
    print("\nT3: Search for known item")
    r = run_script(["--search", "PicassoTiles"])
    check("T3: exit code 0", r.returncode == 0, f"exit {r.returncode}: {r.stderr[:200]}")

    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("T3: has search_term", data.get("search_term") == "PicassoTiles")
        check("T3: found matches", data.get("count", 0) > 0,
              f"count={data.get('count')}")

    print("\nT4: Search for nonexistent item")
    r = run_script(["--search", "xyznonexistentproduct123"])
    check("T4: exit code 0", r.returncode == 0, f"exit {r.returncode}: {r.stderr[:200]}")

    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("T4: count is 0", data.get("count") == 0)


def test_subscriptions():
    """T5: Subscriptions page."""
    print("\nT5: Subscriptions")
    r = run_script(["--subscriptions"])
    check("T5: exit code 0", r.returncode == 0, f"exit {r.returncode}: {r.stderr[:200]}")

    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("T5: has query field", data.get("query") == "subscriptions")
        check("T5: has text_preview or page_length",
              data.get("text_preview") is not None or data.get("page_length") is not None)


def test_cache():
    """T6: Second call uses cache."""
    print("\nT6: Cache reuse")
    start = time.time()
    r1 = run_script(["--recent", "--days", "30"])
    first_time = time.time() - start

    start = time.time()
    r2 = run_script(["--recent", "--days", "30"])
    second_time = time.time() - start

    check("T6: both calls succeed", r1.returncode == 0 and r2.returncode == 0)
    # Both calls should be fast (cache hit) and return identical data
    check("T6: both calls fast", first_time < 5.0 and second_time < 5.0,
          f"first={first_time:.1f}s second={second_time:.1f}s")

    if r1.returncode == 0 and r2.returncode == 0:
        d1 = json.loads(r1.stdout)
        d2 = json.loads(r2.stdout)
        check("T6: same data returned", d1.get("count") == d2.get("count"))


if __name__ == "__main__":
    print("=" * 50)
    print("Hilda Hippo -- Amazon Orders E2E Tests")
    print("=" * 50)

    # Check prerequisites
    if not os.path.exists(SCRIPT):
        print(f"ERROR: Script not found at {SCRIPT}")
        sys.exit(1)

    if not os.environ.get("AMAZON_USERNAME"):
        print("ERROR: AMAZON_USERNAME not set in environment")
        sys.exit(1)

    test_recent()
    test_search()
    test_subscriptions()
    test_cache()

    print("\n" + "=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 50)

    sys.exit(1 if failed else 0)
