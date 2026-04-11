/**
 * messages-extract.js — Google Messages contact mining via Chrome DevTools
 *
 * Paste this entire script into the Chrome DevTools console on messages.google.com.
 * It walks through all conversations, extracts messages, and copies the result
 * to your clipboard as JSON.
 *
 * Based on selector patterns from Flux chrome_extension/content_scripts/google_messages.js
 *
 * Usage:
 *   1. Open messages.google.com in Chrome
 *   2. Press F12 to open DevTools
 *   3. Go to Console tab
 *   4. Paste this entire script and press Enter
 *   5. Wait for it to finish (progress shown in console)
 *   6. Result is copied to clipboard — paste into cache/mined-messages.json
 */

(async function mineMessages() {
  "use strict";

  const SELECTORS = {
    conversationList: [
      "mws-conversations-list",
      '[role="listbox"]',
      "nav [role='list']",
    ],
    conversationItem: [
      "mws-conversation-list-item",
      "a[href*='conversation']",
      '[role="option"]',
    ],
    conversationName: [
      "mws-conversation-list-item-content .name",
      "[data-e2e-conversation-name]",
      "h3.name",
      ".text-content .name",
    ],
    conversationSnippet: [
      "mws-conversation-list-item-content .snippet-text",
      ".text-content .snippet-text",
      ".snippet",
    ],
    conversationTimestamp: [
      "mws-relative-timestamp",
      ".timestamp",
      "time",
    ],
    messageList: [
      "mws-messages-list",
      "div[data-e2e-message-list]",
      '[role="list"]',
    ],
    messageWrapper: [
      "mws-message-wrapper",
      "[data-e2e-message]",
      ".message-wrapper",
    ],
    messageText: [
      "mws-message-content .text-msg",
      "[data-e2e-message-text]",
      ".message-text",
      ".text-content",
    ],
    messageTimestamp: [
      "mws-relative-timestamp",
      "[data-e2e-timestamp]",
      ".timestamp",
    ],
    headerName: [
      "mws-conversation-header h2",
      "[data-e2e-conversation-name]",
      "header h2",
    ],
    backButton: [
      'mws-conversation-back button',
      'button[aria-label="Back"]',
      'a[href="/"]',
    ],
  };

  function $(selectors, root = document) {
    for (const sel of selectors) {
      const el = root.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function $$(selectors, root = document) {
    for (const sel of selectors) {
      const els = root.querySelectorAll(sel);
      if (els.length > 0) return Array.from(els);
    }
    return [];
  }

  function text(el) {
    return el ? el.textContent.trim() : "";
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  console.log("🐱🤝 Mining Google Messages...");

  // Step 1: Get all conversation items from the sidebar
  const convItems = $$(SELECTORS.conversationItem);
  console.log(`Found ${convItems.length} conversations`);

  if (convItems.length === 0) {
    console.error("No conversations found. Make sure you're on messages.google.com with conversations visible.");
    return;
  }

  const results = [];

  for (let i = 0; i < convItems.length; i++) {
    const item = convItems[i];

    // Get name and snippet from sidebar (before clicking)
    const nameEl = $(SELECTORS.conversationName, item);
    const snippetEl = $(SELECTORS.conversationSnippet, item);
    const tsEl = $(SELECTORS.conversationTimestamp, item);

    const contactName = text(nameEl) || `Conversation ${i + 1}`;
    const lastSnippet = text(snippetEl);
    const lastTimestamp = text(tsEl);

    console.log(`  [${i + 1}/${convItems.length}] ${contactName}`);

    // Click into the conversation
    item.click();
    await sleep(1500); // Wait for messages to load

    // Read messages from the conversation
    const messages = [];
    const msgWrappers = $$(SELECTORS.messageWrapper);

    for (const wrapper of msgWrappers) {
      const msgTextEl = $(SELECTORS.messageText, wrapper);
      const msgTsEl = $(SELECTORS.messageTimestamp, wrapper);

      const msgText = text(msgTextEl);
      const msgTs = text(msgTsEl);

      if (!msgText) continue;

      // Determine direction: outgoing messages typically have specific classes
      const isOutbound =
        wrapper.classList.contains("outgoing") ||
        wrapper.closest(".outgoing") !== null ||
        wrapper.querySelector('[data-e2e-is-outgoing="true"]') !== null ||
        wrapper.closest('[data-outgoing="true"]') !== null;

      messages.push({
        direction: isOutbound ? "outbound" : "inbound",
        text: msgText,
        timestamp: msgTs || null,
      });
    }

    results.push({
      name: contactName,
      platform: "sms",
      messages: messages,
      message_count: messages.length,
      last_snippet: lastSnippet,
      last_timestamp: lastTimestamp,
    });

    // Go back to conversation list
    const backBtn = $(SELECTORS.backButton);
    if (backBtn) {
      backBtn.click();
      await sleep(800);
    } else {
      // Try browser back
      window.history.back();
      await sleep(800);
    }
  }

  // Build final output
  const output = {
    status: "ok",
    source: "messages",
    mined_at: new Date().toISOString(),
    conversations_scanned: results.length,
    contacts_found: results.length,
    contacts: results,
  };

  // Copy to clipboard
  const jsonStr = JSON.stringify(output, null, 2);
  try {
    await navigator.clipboard.writeText(jsonStr);
    console.log(`\n🐱🤝 Done! ${results.length} conversations mined.`);
    console.log("Result copied to clipboard. Paste into cache/mined-messages.json");
  } catch (e) {
    console.log("Clipboard failed. Output below — copy manually:");
    console.log(jsonStr);
  }

  return output;
})();
