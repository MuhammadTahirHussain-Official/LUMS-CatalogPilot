// Pip, the catalog helper. A small companion that explains each screen, suggests the next
// step, answers common questions and reacts to what happens: pleased when things work,
// sad when something fails, stern when an action would be unsafe.
//
// Pip is scripted, not a language model: every answer below is written by hand, runs in the
// browser and sends nothing anywhere.

import { h, svg } from "./ui.js";

const FACES = {
  // eyes, mouth and extras per mood; coordinates are in a 64x64 box
  happy: { eyes: "open", mouth: "M24 40 Q32 48 40 40", brows: null },
  thinking: { eyes: "side", mouth: "M27 42 Q32 40 37 42", brows: "M22 21 L28 20 M36 19 L42 21" },
  sad: { eyes: "down", mouth: "M24 45 Q32 38 40 45", brows: "M21 22 L28 19 M36 19 L43 22", tear: true },
  stern: { eyes: "narrow", mouth: "M25 44 L39 44", brows: "M21 18 L29 23 M43 18 L35 23" },
  cheer: { eyes: "closed", mouth: "M22 39 Q32 52 42 39 Z", brows: null, sparkle: true },
  sleepy: { eyes: "closed", mouth: "M28 43 Q32 45 36 43", brows: null },
};

function drawFace(mood) {
  const f = FACES[mood] || FACES.happy;
  const parts = [
    // ears, body, belly
    svg("path", { d: "M14 20 L10 6 L24 13 Z", class: "pip-ear" }), svg("path", { d: "M50 20 L54 6 L40 13 Z", class: "pip-ear" }),
    svg("ellipse", { cx: 32, cy: 35, rx: 24, ry: 23, class: "pip-body" }),
    svg("ellipse", { cx: 32, cy: 44, rx: 13, ry: 10, class: "pip-belly" }),
    svg("ellipse", { cx: 17, cy: 40, rx: 4, ry: 2.5, class: "pip-cheek" }), svg("ellipse", { cx: 47, cy: 40, rx: 4, ry: 2.5, class: "pip-cheek" }),
  ];
  if (f.eyes === "closed") {
    parts.push(svg("path", { d: "M21 31 Q25 27 29 31 M35 31 Q39 27 43 31", class: "pip-line" }));
  } else if (f.eyes === "narrow") {
    parts.push(svg("path", { d: "M21 30 L29 30 M35 30 L43 30", class: "pip-line thick" }));
  } else {
    const dx = f.eyes === "side" ? 1.5 : 0;
    const dy = f.eyes === "down" ? 1.5 : 0;
    parts.push(
      svg("circle", { cx: 25, cy: 30, r: 4.5, class: "pip-eye" }), svg("circle", { cx: 39, cy: 30, r: 4.5, class: "pip-eye" }),
      svg("circle", { cx: 25 + dx, cy: 30 + dy, r: 2.2, class: "pip-pupil" }), svg("circle", { cx: 39 + dx, cy: 30 + dy, r: 2.2, class: "pip-pupil" }));
  }
  if (f.brows) parts.push(svg("path", { d: f.brows, class: "pip-line" }));
  parts.push(svg("path", { d: f.mouth, class: mood === "cheer" ? "pip-mouth-open" : "pip-line" }));
  if (f.tear) parts.push(svg("path", { d: "M44 35 Q46 40 44 42 Q42 40 44 35 Z", class: "pip-tear" }));
  if (f.sparkle) parts.push(svg("path", { d: "M54 22 l2 4 4 2 -4 2 -2 4 -2 -4 -4 -2 4 -2 Z M8 26 l1.5 3 3 1.5 -3 1.5 -1.5 3 -1.5 -3 -3 -1.5 3 -1.5 Z", class: "pip-spark" }));
  return svg("svg", { viewBox: "0 0 64 64", class: `pip-face mood-${mood}`, "aria-hidden": "true", focusable: "false" }, parts);
}

// What Pip says on each screen: what it is for, what to do, what to avoid.
const PAGES = {
  overview: { tip: "This is your home base. The checklist shows what to do next.",
    dont: "No need to hunt through the menu: start with “Move a store” and I'll walk you through it." },
  migrate: { tip: "I'll take you from your old store to your new one, one step at a time. You can leave and come back; I remember where you were.",
    dont: "Don't close a step halfway worrying it breaks things. Nothing is sent to your new store until you approve the preview." },
  catalog: { tip: "These are your products, kept safely here. Click a title to edit it. “History” can undo any change.",
    dont: "Only products marked Active get published, so don't forget to activate the ones you want live." },
  uploads: { tip: "Got a spreadsheet instead of a store? Drop it here. I check every file for threats before reading it.",
    dont: "Don't upload files with macros (.xlsm) or programs. I'll refuse them to keep you safe." },
  review: { tip: "These are suggested improvements. Green is the new version. Approve, edit or reject each one.",
    dont: "Don't approve “High impact” changes without reading them: they affect category, price or stock." },
  channels: { tip: "A channel is a store you read from or publish to. Add one, then connect it with a key made for apps.",
    dont: "Never paste your store login password or a card number. I only accept API keys, and I'll stop you if it looks wrong." },
  jobs: { tip: "Every publish starts with a preview of exactly what will change. Nothing happens until you approve.",
    dont: "Don't panic if progress pauses: I retry on my own, and finished items are never sent twice." },
  activity: { tip: "A permanent record of who did what. “Verify integrity” proves nobody edited it.", dont: null },
  settings: { tip: "Security and team settings live here. Two-step sign-in is the best five minutes you'll spend.",
    dont: "Don't share your account. Invite people instead, so each has their own access." },
};

const FAQ = [
  { q: "What should I do next?", keys: ["next", "start", "begin", "do now", "how do i"], a: null },
  { q: "Is my store safe?", keys: ["safe", "secure", "security", "trust", "hack", "password", "key"],
    a: "Yes. Store keys are encrypted the moment you save them and can never be shown again, even to you. Every use of a key is logged under Channels → Key details. Nothing is published without a preview and your approval, and every publish can be rolled back." },
  { q: "What if my internet drops?", keys: ["offline", "internet", "connection", "crash", "close", "lost", "disconnect", "laptop"],
    a: "Your work continues on the server. Imports and publishing save their place after every step, so they resume exactly where they stopped, and nothing is sent twice. Just reopen this page." },
  { q: "Will I get duplicates?", keys: ["duplicate", "twice", "double", "again", "re-run", "rerun"],
    a: "No. Each product is matched by its identity in the source store, so importing or publishing again updates what exists instead of creating copies." },
  { q: "Can I undo a publish?", keys: ["undo", "rollback", "roll back", "revert", "mistake", "wrong"],
    a: "Yes. Open the finished job under Publishing and choose “Roll back”. You'll see what would be removed or restored before anything happens. Catalog edits can be undone from Catalog → History." },
  { q: "What does the AI change?", keys: ["ai", "suggest", "improve", "change my", "automatic", "title"],
    a: "It tidies titles and descriptions, finds attributes and suggests categories and search text, using only words already in your product. It never touches prices or stock. Risky or uncertain suggestions wait for your review." },
  { q: "What shouldn't I do?", keys: ["avoid", "shouldn't", "should not", "don't", "dont", "careful", "not do"], a: null },
];

let panel = null;
let bubble = null;
let faceHolder = null;
let currentRoute = "overview";
let nextStep = null;
let moodTimer = null;
let bubbleTimer = null;

function setMood(mood, holdMs = 0) {
  if (!faceHolder) return;
  faceHolder.replaceChildren(drawFace(mood));
  clearTimeout(moodTimer);
  if (holdMs) moodTimer = setTimeout(() => setMood("happy"), holdMs);
}

function say(text, mood = "happy", holdMs = 7000) {
  if (!bubble) return;
  setMood(mood, holdMs);
  bubble.textContent = text;
  bubble.hidden = false;
  clearTimeout(bubbleTimer);
  bubbleTimer = setTimeout(() => { bubble.hidden = true; }, holdMs);
}

function answer(question) {
  const page = PAGES[currentRoute] || PAGES.overview;
  const text = question.toLowerCase();
  const hit = FAQ.find((f) => f.keys.some((k) => text.includes(k)));
  if (!hit) {
    return { mood: "thinking", text: "I'm a simple helper, so I may not know that one. Here's what this page is for: " + page.tip };
  }
  if (hit.q === "What should I do next?") {
    return { mood: "happy", text: nextStep ? `Next up: ${nextStep.label}.` : "You're all set! Try “Move a store” to bring in another catalog.", link: nextStep };
  }
  if (hit.q === "What shouldn't I do?") return { mood: "stern", text: page.dont || "Nothing risky here. Look around freely." };
  return { mood: "happy", text: hit.a };
}

function openPanel() {
  if (panel) { panel.remove(); panel = null; return; }
  bubble.hidden = true;
  const page = PAGES[currentRoute] || PAGES.overview;
  const log = h("div", { class: "pip-log", role: "log", "aria-live": "polite" },
    h("p", { class: "pip-msg" }, page.tip), page.dont ? h("p", { class: "pip-msg dont" }, h("strong", {}, "Heads up: "), page.dont) : null);
  const reply = (question) => {
    const r = answer(question);
    setMood(r.mood, 6000);
    const asked = h("p", { class: "pip-msg me" }, question);
    log.append(asked, h("p", { class: "pip-msg" }, r.text,
      r.link ? h("a", { class: "button primary small-btn", href: `#/${r.link.route}`, onclick: () => { panel.remove(); panel = null; } }, "Take me there") : null));
    asked.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  const input = h("input", { type: "text", maxlength: "200", placeholder: "Ask me something…", "aria-label": "Ask Pip a question" });
  panel = h("section", { class: "pip-panel", role: "dialog", "aria-label": "Pip, your helper" },
    h("header", {}, h("strong", {}, "Pip"), h("span", { class: "muted small" }, "your catalog helper"),
      h("button", { class: "icon", "aria-label": "Close helper", onclick: () => { panel.remove(); panel = null; } }, "×")),
    log,
    h("div", { class: "pip-chips" }, FAQ.map((f) => h("button", { class: "chip", onclick: () => reply(f.q) }, f.q))),
    h("form", { class: "row", onsubmit: (event) => { event.preventDefault(); if (input.value.trim()) { reply(input.value.trim()); input.value = ""; } } },
      input, h("button", { type: "submit" }, "Ask")));
  document.body.append(panel);
  input.focus();
}

export function mountPip() {
  if (document.getElementById("pip")) return;
  faceHolder = h("span", { class: "pip-holder" });
  bubble = h("div", { class: "pip-bubble", role: "status", hidden: true });
  const button = h("button", { id: "pip", class: "pip-button", "aria-label": "Open Pip, your helper", onclick: openPanel }, faceHolder);
  document.body.append(bubble, button);
  setMood("happy");

  window.addEventListener("cp:feedback", (event) => {
    const { kind, message, code } = event.detail;
    if (kind === "good") { say("Nice! " + message, "cheer", 5000); return; }
    if (kind !== "error") return;
    if (code === "credential_type_refused") {
      say("Stop! That looks like a password or card detail. Never paste those here. Create an API key in your store's admin instead.", "stern", 12000);
    } else if (code === "forbidden" || code === "step_up_required" || code === "tenant_frozen") {
      say("I can't let that through. " + message, "stern", 9000);
    } else if (code === "network") {
      say("I can't reach the server. Your work is safe; I'll keep trying.", "sad", 9000);
    } else {
      say("Oh no. " + message, "sad", 9000);
    }
  });
  window.addEventListener("offline", () => say("You're offline. Anything already running continues on the server, and I'll catch up when you're back.", "sad", 15000));
  window.addEventListener("online", () => say("Back online! Everything is where you left it.", "cheer", 5000));
  let idle;
  const wake = () => { clearTimeout(idle); idle = setTimeout(() => setMood("sleepy"), 120000); };
  ["pointerdown", "keydown"].forEach((name) => window.addEventListener(name, () => { if (faceHolder.querySelector(".mood-sleepy")) setMood("happy"); wake(); }, { passive: true }));
  wake();
}

export function unmountPip() {
  for (const el of [document.getElementById("pip"), bubble, panel]) el?.remove();
  bubble = panel = faceHolder = null;
}

// Called by the router on every navigation.
export function pipOnRoute(route, next, firstVisit) {
  currentRoute = route;
  nextStep = next;
  if (panel) { panel.remove(); panel = null; }
  if (firstVisit && PAGES[route]) say(PAGES[route].tip, "happy", 9000);
}

export function pipSay(text, mood = "happy", holdMs = 7000) { say(text, mood, holdMs); }
