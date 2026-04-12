/**
 * messages-extract.js — Google Messages full conversation mining v4
 *
 * Paste into Chrome DevTools Console on messages.google.com.
 *
 * Fixed: uses Angular router links (click <a role="option">) instead of
 * window.location.href which killed the script via full page reload.
 * Waits for mws-message-wrapper elements to appear after navigation.
 */

(async function mineMessages() {
  "use strict";

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  console.log("🐱🤝 Google Messages Miner v4");

  // ── Get all conversation <a> links ─────────────────────────
  // From probe: <a role="option" class="list-item" href="/web/conversations/...">
  const getConvLinks = () => document.querySelectorAll('a[role="option"][data-e2e-conversation]');

  let convLinks = getConvLinks();
  console.log(`Found ${convLinks.length} conversations`);

  if (convLinks.length === 0) {
    console.error("No conversations. Are you on messages.google.com?");
    return;
  }

  // ── Index sidebar metadata ─────────────────────────────────
  const convMeta = [];
  for (const link of convLinks) {
    // Name: first non-empty span text
    const spans = link.querySelectorAll("span");
    let name = "";
    for (const sp of spans) {
      const t = sp.textContent.trim();
      if (t.length > 0 && t.length < 100 && !name) { name = t; break; }
    }

    // Snippet
    const snippetEl = link.querySelector("mws-conversation-snippet");
    const snippet = snippetEl ? snippetEl.textContent.trim() : "";

    // Timestamp
    const tsEl = link.querySelector("mws-relative-timestamp");
    const ts = tsEl ? tsEl.textContent.trim() : "";

    convMeta.push({ name: name || `Conv ${convMeta.length + 1}`, snippet, timestamp: ts });
  }

  console.log(`Indexed ${convMeta.length} conversations`);

  // ── Walk each conversation ─────────────────────────────────
  const results = [];

  for (let i = 0; i < convMeta.length; i++) {
    const meta = convMeta[i];
    console.log(`  [${i + 1}/${convMeta.length}] ${meta.name}`);

    // Re-query links (DOM updates after navigation)
    convLinks = getConvLinks();
    if (i >= convLinks.length) {
      console.log(`    ⚠ Only ${convLinks.length} links available, stopping`);
      break;
    }

    // Click the <a> link — Angular router handles SPA navigation
    convLinks[i].click();

    // Wait for message list to render
    let msgList = null;
    for (let attempt = 0; attempt < 20; attempt++) {
      await sleep(300);
      // Look for mws-message-wrapper (from probe: exists as a tag name)
      msgList = document.querySelector("mws-messages-list");
      if (msgList) break;
    }

    const messages = [];

    if (msgList) {
      // Google Messages uses virtual scrolling: only ~25 messages in DOM at once.
      // Must extract-while-scrolling: read visible messages, scroll up, repeat.
      const seen = new Set();
      const scrollable = msgList.parentElement || msgList.closest('[style*="overflow"]') || msgList;

      function harvestVisible() {
        const wrappers = msgList.querySelectorAll("mws-message-wrapper");
        for (const w of wrappers) {
          const textEl = w.querySelector(".text-msg");
          const msgText = textEl ? textEl.textContent.trim() : "";
          if (!msgText) continue;

          // Dedupe by text content (virtual scroll recycles elements)
          const key = msgText.substring(0, 100);
          if (seen.has(key)) continue;
          seen.add(key);

          const isOut = w.classList.contains("outgoing") ||
                        w.getAttribute("data-e2e-is-outgoing") === "true" ||
                        w.querySelector('[data-e2e-is-outgoing="true"]') !== null;

          const tsEl = w.querySelector("mws-relative-timestamp");
          const ts = tsEl ? tsEl.textContent.trim() : null;

          messages.push({
            direction: isOut ? "outbound" : "inbound",
            text: msgText.substring(0, 500),
            timestamp: ts,
          });
        }
      }

      // First harvest: bottom of conversation (most recent)
      harvestVisible();

      // Scroll up incrementally (one viewport at a time) and harvest
      let prevSeen = 0;
      let stableRounds = 0;
      const pageHeight = scrollable.clientHeight || 600;
      for (let s = 0; s < 500; s++) {
        scrollable.scrollTop = Math.max(0, scrollable.scrollTop - pageHeight);
        await sleep(400);
        harvestVisible();

        if (seen.size === prevSeen) {
          stableRounds++;
          if (stableRounds >= 5) break;
        } else {
          stableRounds = 0;
        }
        prevSeen = seen.size;
      }
    } else {
      console.log("    ⚠ mws-messages-list not found after 6s");
    }

    results.push({
      name: meta.name,
      platform: "sms",
      messages,
      message_count: messages.length,
      last_snippet: meta.snippet,
      last_timestamp: meta.timestamp,
    });

    if (messages.length > 0) {
      console.log(`    ✓ ${messages.length} messages`);
    } else {
      console.log(`    ✗ 0 messages`);
    }

    // Navigate back: click the logo/back link
    const backLink = document.querySelector('a[data-e2e-messages-title]') ||
                     document.querySelector('a[href="/web/conversations"]') ||
                     document.querySelector("mw-main-nav a");
    if (backLink) {
      backLink.click();
    } else {
      window.history.back();
    }
    await sleep(1000);
  }

  // ── Output ─────────────────────────────────────────────────
  const totalMsgs = results.reduce((s, r) => s + r.message_count, 0);
  const output = {
    status: "ok",
    source: "messages",
    mined_at: new Date().toISOString(),
    conversations_scanned: results.length,
    contacts_found: results.length,
    total_messages: totalMsgs,
    contacts: results,
  };

  const jsonStr = JSON.stringify(output, null, 2);
  try {
    await navigator.clipboard.writeText(jsonStr);
    console.log(`\n🐱🤝 Done! ${results.length} conversations, ${totalMsgs} messages.`);
    console.log("Copied to clipboard — paste into cache/mined-messages.json");
  } catch (e) {
    const blob = new Blob([jsonStr], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "mined-messages.json";
    a.click();
    URL.revokeObjectURL(url);
    console.log(`\n🐱🤝 Done! Downloaded as mined-messages.json`);
  }

  return output;
})();
