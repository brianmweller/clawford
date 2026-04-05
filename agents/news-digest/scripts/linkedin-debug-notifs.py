#!/usr/bin/env python3
"""Debug script to find LinkedIn notification selectors."""
from playwright.sync_api import sync_playwright
import time
import json
import re

PROFILE = "/home/node/.openclaw/news-digest-workspace/linkedin-profile"
CACHE = "/home/node/.openclaw/news-digest-workspace/cache"

JS_EXTRACT = """() => {
    var results = [];

    // Method 1: artdeco-card elements
    var cards = document.querySelectorAll('.artdeco-card');
    for (var i = 0; i < cards.length; i++) {
        var card = cards[i];
        var text = (card.innerText || '').trim();
        if (text.length > 20 && text.length < 500) {
            results.push({
                method: 'artdeco-card',
                text: text.substring(0, 200),
                classes: card.className.substring(0, 100)
            });
        }
    }

    // Method 2: list items
    var items = document.querySelectorAll('li');
    for (var j = 0; j < items.length; j++) {
        var item = items[j];
        var t = (item.innerText || '').trim();
        if (t.length > 30 && t.length < 400 && t.indexOf('ago') > -1) {
            results.push({
                method: 'li-with-ago',
                text: t.substring(0, 200),
                classes: item.className.substring(0, 100)
            });
        }
    }

    // Method 3: anything with notification-related content
    var all = document.querySelectorAll('*');
    for (var k = 0; k < all.length && k < 5000; k++) {
        var el = all[k];
        var txt = '';
        for (var c = 0; c < el.childNodes.length; c++) {
            if (el.childNodes[c].nodeType === 3) {
                txt += el.childNodes[c].textContent;
            }
        }
        txt = txt.trim();
        if (txt.length > 20 && (txt.indexOf('viewed') > -1 || txt.indexOf('liked') > -1 || txt.indexOf('commented') > -1 || txt.indexOf('reacted') > -1 || txt.indexOf('posted') > -1)) {
            results.push({
                method: 'text-match',
                text: txt.substring(0, 200),
                tag: el.tagName,
                classes: el.className.substring(0, 100)
            });
        }
    }

    return results.slice(0, 20);
}"""

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE,
        headless=True,
        args=["--no-sandbox", "--disable-gpu"],
        executable_path="/usr/bin/chromium",
    )
    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto("https://www.linkedin.com/notifications/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)

    notifs = page.evaluate(JS_EXTRACT)
    print(json.dumps(notifs, indent=2))

    browser.close()
