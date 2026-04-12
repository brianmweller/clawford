/**
 * messages-probe.js — Dump Google Messages DOM structure for debugging.
 *
 * Paste in Chrome DevTools Console on messages.google.com.
 * Clicks into the first conversation and dumps the DOM tree so we can
 * find the right selectors for message extraction.
 */

(async function probeMessages() {
  "use strict";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Step 1: Find and click the first conversation
  console.log("=== SIDEBAR PROBE ===");

  // Dump all unique tag names in the sidebar
  const sidebar = document.querySelector("nav") || document.querySelector('[role="navigation"]') || document.querySelector("aside") || document.body;
  const sidebarTags = new Set();
  sidebar.querySelectorAll("*").forEach(el => sidebarTags.add(el.tagName.toLowerCase()));
  console.log("Sidebar tag names:", [...sidebarTags].sort().join(", "));

  // Find clickable conversation items
  const candidates = [
    { sel: "mws-conversation-list-item", els: document.querySelectorAll("mws-conversation-list-item") },
    { sel: "a[href*='conversation']", els: document.querySelectorAll("a[href*='conversation']") },
    { sel: '[role="listbox"] > *', els: document.querySelectorAll('[role="listbox"] > *') },
    { sel: '[role="option"]', els: document.querySelectorAll('[role="option"]') },
    { sel: '[role="listitem"]', els: document.querySelectorAll('[role="listitem"]') },
    { sel: "a[href*='thread']", els: document.querySelectorAll("a[href*='thread']") },
  ];

  for (const c of candidates) {
    if (c.els.length > 0) {
      console.log(`  ${c.sel}: ${c.els.length} elements`);
      // Dump first element's structure
      const first = c.els[0];
      console.log(`    tagName: ${first.tagName}`);
      console.log(`    className: ${first.className}`);
      console.log(`    attributes:`, [...first.attributes].map(a => `${a.name}="${a.value.substring(0, 50)}"`).join(", "));
      console.log(`    innerHTML preview:`, first.innerHTML.substring(0, 300));
    }
  }

  // Find the first clickable item
  let clickTarget = null;
  for (const c of candidates) {
    if (c.els.length > 0) {
      clickTarget = c.els[0];
      console.log(`\nClicking: ${c.sel} (${clickTarget.tagName})`);
      break;
    }
  }

  if (!clickTarget) {
    // Broader search: any element with conversation-like text
    console.log("\nNo standard selectors found. Trying broader search...");
    const allLinks = document.querySelectorAll("a[href]");
    console.log(`  All links: ${allLinks.length}`);
    for (const a of [...allLinks].slice(0, 10)) {
      console.log(`    ${a.href.substring(0, 80)} → ${a.textContent.substring(0, 50).trim()}`);
    }
    return;
  }

  clickTarget.click();
  await sleep(2500);

  // Step 2: Dump the message area DOM
  console.log("\n=== MESSAGE AREA PROBE ===");

  const mainArea = document.querySelector("main") || document.querySelector('[role="main"]') || document.querySelector("#main-content");
  if (!mainArea) {
    console.log("No main area found. Checking full page...");
    const allTags = new Set();
    document.querySelectorAll("*").forEach(el => allTags.add(el.tagName.toLowerCase()));
    console.log("All tag names:", [...allTags].sort().join(", "));
    return;
  }

  console.log(`Main area: <${mainArea.tagName.toLowerCase()} class="${mainArea.className}">`);

  // Dump unique tag names in main area
  const mainTags = new Set();
  mainArea.querySelectorAll("*").forEach(el => mainTags.add(el.tagName.toLowerCase()));
  console.log("Main area tags:", [...mainTags].sort().join(", "));

  // Look for message-like containers
  const msgCandidates = [
    "mws-message-wrapper",
    "mws-message",
    "mws-message-part",
    '[role="listitem"]',
    '[role="row"]',
    '[data-e2e-message]',
    ".message-wrapper",
    ".message",
  ];

  for (const sel of msgCandidates) {
    const els = mainArea.querySelectorAll(sel);
    if (els.length > 0) {
      console.log(`\n  ${sel}: ${els.length} elements`);
      const first = els[0];
      console.log(`    tagName: ${first.tagName}`);
      console.log(`    className: ${first.className}`);
      console.log(`    attributes:`, [...first.attributes].map(a => `${a.name}="${a.value.substring(0, 80)}"`).join(", "));
      console.log(`    textContent preview:`, first.textContent.substring(0, 200).trim());
      console.log(`    children: ${first.children.length} (tags: ${[...new Set([...first.children].map(c => c.tagName.toLowerCase()))].join(", ")})`);
      // Check for shadow roots
      if (first.shadowRoot) {
        console.log(`    HAS SHADOW ROOT — children: ${first.shadowRoot.children.length}`);
        console.log(`    Shadow innerHTML:`, first.shadowRoot.innerHTML.substring(0, 300));
      }
    }
  }

  // Dump the tree structure of main area (first 3 levels)
  console.log("\n=== MAIN AREA TREE (3 levels) ===");
  function dumpTree(el, depth = 0, maxDepth = 3) {
    if (depth >= maxDepth) return;
    const indent = "  ".repeat(depth);
    const tag = el.tagName.toLowerCase();
    const cls = el.className ? `.${el.className.toString().split(" ").slice(0, 3).join(".")}` : "";
    const role = el.getAttribute("role") ? `[role="${el.getAttribute("role")}"]` : "";
    const dataTestId = el.getAttribute("data-testid") ? `[data-testid="${el.getAttribute("data-testid")}"]` : "";
    const shadow = el.shadowRoot ? " [SHADOW]" : "";
    const text = el.children.length === 0 ? ` "${el.textContent.substring(0, 50).trim()}"` : ` (${el.children.length} children)`;
    console.log(`${indent}<${tag}${cls}${role}${dataTestId}${shadow}>${text}`);
    for (const child of el.children) {
      dumpTree(child, depth + 1, maxDepth);
    }
    if (el.shadowRoot) {
      console.log(`${indent}  [SHADOW ROOT]`);
      for (const child of el.shadowRoot.children) {
        dumpTree(child, depth + 1, maxDepth);
      }
    }
  }

  dumpTree(mainArea, 0, 4);

  // Step 3: Try to find actual message text in the page
  console.log("\n=== TEXT CONTENT SEARCH ===");
  const walker = document.createTreeWalker(mainArea, NodeFilter.SHOW_TEXT, null, false);
  const textNodes = [];
  let node;
  while ((node = walker.nextNode())) {
    const t = node.textContent.trim();
    if (t.length > 10 && t.length < 500) {
      textNodes.push({
        text: t.substring(0, 100),
        parentTag: node.parentElement?.tagName,
        parentClass: node.parentElement?.className?.toString().substring(0, 60),
        grandparentTag: node.parentElement?.parentElement?.tagName,
      });
    }
  }
  console.log(`Found ${textNodes.length} text nodes with 10-500 chars`);
  for (const tn of textNodes.slice(0, 15)) {
    console.log(`  <${tn.parentTag} class="${tn.parentClass}"> "${tn.text}"`);
  }

  console.log("\n=== PROBE COMPLETE ===");
  console.log("Copy everything above and share it so I can write exact selectors.");
})();
