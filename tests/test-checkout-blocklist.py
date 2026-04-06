#!/usr/bin/env python3
"""Tests for the checkout blocklist in amazon-cart.py and costco-cart.py.

These verify that the CHECKOUT_BLOCKLIST is present and covers all
expected checkout-related strings. The blocklist is the code-level
safety gate — if it's missing or incomplete, Hilda could proceed
to checkout.

Does NOT require Playwright or any network access.
"""

import importlib.util
import os
import sys

AMAZON_CART = os.path.join(os.path.dirname(__file__), "..", "agents", "shopping", "scripts", "amazon-cart.py")
COSTCO_CART = os.path.join(os.path.dirname(__file__), "..", "agents", "shopping", "scripts", "costco-cart.py")

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


def load_module(path, name):
    """Load a Python module from a file path without executing main()."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)

    # Prevent auto-imports of playwright/openai from failing
    original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def mock_import(name, *args, **kwargs):
        if name in ("playwright", "playwright.sync_api", "openai", "amazonorders",
                     "amazonorders.session", "amazonorders.orders", "amazonorders.exception"):
            raise ImportError(f"Mocked: {name}")
        return original_import(name, *args, **kwargs)

    import builtins
    old = builtins.__import__
    builtins.__import__ = mock_import
    try:
        spec.loader.exec_module(mod)
    except ImportError:
        pass
    finally:
        builtins.__import__ = old

    return mod


# Required strings that MUST appear in every blocklist
REQUIRED_BLOCKED = [
    "place your order",
    "place order",
    "buy now",
    "proceed to checkout",
    "confirm purchase",
    "submit order",
    "complete purchase",
    "pay now",
]


def test_amazon_blocklist():
    """T1: Amazon cart script has complete checkout blocklist."""
    print("\nT1: Amazon CHECKOUT_BLOCKLIST")

    # Read the file as text to find CHECKOUT_BLOCKLIST
    with open(AMAZON_CART, "r") as f:
        source = f.read()

    check("CHECKOUT_BLOCKLIST defined", "CHECKOUT_BLOCKLIST" in source)
    check("safe_click function exists", "def safe_click" in source)

    # Extract the blocklist values from source
    source_lower = source.lower()
    for phrase in REQUIRED_BLOCKED:
        check(f'blocks "{phrase}"', phrase in source_lower,
              f'"{phrase}" not found in blocklist')

    # Verify NO checkout-related code paths exist
    check('no "proceed_to_checkout" function', "def proceed_to_checkout" not in source_lower)
    check('no "complete_purchase" function', "def complete_purchase" not in source_lower)
    check('no "place_order" function', "def place_order" not in source_lower)


def test_costco_blocklist():
    """T2: Costco cart script has complete checkout blocklist."""
    print("\nT2: Costco CHECKOUT_BLOCKLIST")

    with open(COSTCO_CART, "r") as f:
        source = f.read()

    check("CHECKOUT_BLOCKLIST defined", "CHECKOUT_BLOCKLIST" in source)
    check("safe_click function exists", "def safe_click" in source)

    source_lower = source.lower()
    for phrase in REQUIRED_BLOCKED:
        check(f'blocks "{phrase}"', phrase in source_lower,
              f'"{phrase}" not found in blocklist')

    check('no "proceed_to_checkout" function', "def proceed_to_checkout" not in source_lower)
    check('no "complete_purchase" function', "def complete_purchase" not in source_lower)
    check('no "place_order" function', "def place_order" not in source_lower)


def test_blocklist_consistency():
    """T3: Both blocklists cover the same phrases."""
    print("\nT3: Blocklist consistency")

    with open(AMAZON_CART, "r") as f:
        amazon_src = f.read()
    with open(COSTCO_CART, "r") as f:
        costco_src = f.read()

    # Extract blocklist entries (crude but effective)
    import re

    def extract_blocklist(src):
        # Find the CHECKOUT_BLOCKLIST assignment
        match = re.search(r'CHECKOUT_BLOCKLIST\s*=\s*\[(.*?)\]', src, re.DOTALL)
        if not match:
            return set()
        entries = re.findall(r'"([^"]+)"', match.group(1))
        return set(e.lower() for e in entries)

    amazon_bl = extract_blocklist(amazon_src)
    costco_bl = extract_blocklist(costco_src)

    check("Amazon blocklist non-empty", len(amazon_bl) > 0, f"got {len(amazon_bl)}")
    check("Costco blocklist non-empty", len(costco_bl) > 0, f"got {len(costco_bl)}")

    # Check that all required phrases are in both
    for phrase in REQUIRED_BLOCKED:
        in_amazon = any(phrase in entry for entry in amazon_bl)
        in_costco = any(phrase in entry for entry in costco_bl)
        check(f'"{phrase}" in both', in_amazon and in_costco,
              f"amazon={in_amazon}, costco={in_costco}")


def test_no_checkout_selectors():
    """T4: Cart scripts don't contain selectors for checkout buttons."""
    print("\nT4: No checkout selectors in click targets")

    for label, path in [("Amazon", AMAZON_CART), ("Costco", COSTCO_CART)]:
        with open(path, "r") as f:
            source = f.read()

        # These selectors should NEVER appear as click targets
        dangerous_selectors = [
            "checkout-button",
            "place-order",
            "buy-now-button",
            "submit-order",
            "confirmPurchase",
        ]
        for sel in dangerous_selectors:
            # Check it's not used in page.click() or safe_click() call
            # (it's OK if it appears in the blocklist text itself)
            lines = source.split("\n")
            for i, line in enumerate(lines):
                if sel in line and ("click(" in line or "safe_click(" in line):
                    if "BLOCKLIST" not in line and "blocklist" not in line:
                        check(f"{label}: no click on '{sel}'", False,
                              f"line {i+1}: {line.strip()}")
                        break
            else:
                check(f"{label}: no click on '{sel}'", True)


def test_audit_logging():
    """T5: Both cart scripts log actions."""
    print("\nT5: Audit logging")

    for label, path in [("Amazon", AMAZON_CART), ("Costco", COSTCO_CART)]:
        with open(path, "r") as f:
            source = f.read()

        check(f"{label}: log_action function exists", "def log_action" in source)
        check(f"{label}: logs to cart-actions.jsonl", "cart-actions.jsonl" in source)
        check(f"{label}: logs blocked clicks", "blocked_click" in source)
        check(f"{label}: logs add_to_cart", '"add_to_cart"' in source)


if __name__ == "__main__":
    print("=" * 50)
    print("Hilda Hippo -- Checkout Blocklist Tests")
    print("=" * 50)

    test_amazon_blocklist()
    test_costco_blocklist()
    test_blocklist_consistency()
    test_no_checkout_selectors()
    test_audit_logging()

    print("\n" + "=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 50)

    sys.exit(1 if failed else 0)
