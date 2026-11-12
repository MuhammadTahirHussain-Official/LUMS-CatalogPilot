// Small DOM toolkit. Everything is built with createElement / textContent, never innerHTML,
// so product data, file names and API messages can never run as markup.

// DOM's own append() turns null into the text "null". Every element made here gets an
// append() that skips empty values, so `cond ? node : null` is safe everywhere.
function appendPresent(...nodes) {
  const present = nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false);
  Element.prototype.append.apply(this, present);
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  el.append = appendPresent;
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "value" || key === "checked" || key === "disabled" || key === "selected" || key === "indeterminate") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export const clear = (el) => { el.replaceChildren(); el.append = appendPresent; return el; };

export function toast(message, kind = "info") {
  const region = document.getElementById("toasts");
  const item = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, message);
  region.append(item);
  setTimeout(() => item.remove(), kind === "error" ? 9000 : 4500);
}

export function showError(error) {
  const extra = error?.problem?.errors;
  const detail = Array.isArray(extra) && extra.length
    ? `${error.message} ${extra.map((e) => (typeof e === "string" ? e : `${e.field}: ${e.code}`)).join("; ")}`
    : error?.message || "Something went wrong.";
  toast(detail, "error");
}

// Runs an async action from a button: disables it while pending and reports failures.
export function action(fn) {
  return async (event) => {
    const button = event?.currentTarget;
    if (button?.disabled) return;
    if (button) { button.disabled = true; button.classList.add("busy"); }
    try { await fn(event); } catch (error) { showError(error); } finally {
      if (button) { button.disabled = false; button.classList.remove("busy"); }
    }
  };
}

export function modal(title, content, { wide = false } = {}) {
  const dialog = h("dialog", { class: wide ? "wide" : "" });
  const close = () => { dialog.close(); dialog.remove(); };
  dialog.append(
    h("header", {}, h("h2", {}, title), h("button", { class: "icon", "aria-label": "Close", onclick: close }, "×")),
    h("div", { class: "modal-body" }, content),
  );
  dialog.addEventListener("cancel", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  return { dialog, close };
}

export function confirmDialog(title, message, confirmLabel = "Confirm", danger = false) {
  return new Promise((resolve) => {
    const m = modal(title, h("div", {},
      h("p", {}, message),
      h("div", { class: "row end" },
        h("button", { onclick: () => { m.close(); resolve(false); } }, "Cancel"),
        h("button", { class: danger ? "danger" : "primary", onclick: () => { m.close(); resolve(true); } }, confirmLabel))));
    m.dialog.addEventListener("cancel", () => resolve(false));
  });
}

export function field(label, input, hint) {
  const id = input.id || `f${Math.random().toString(36).slice(2, 9)}`;
  input.id = id;
  return h("div", { class: "field" }, h("label", { for: id }, label), input, hint ? h("small", {}, hint) : null);
}

export function badge(text, tone = "neutral") { return h("span", { class: `badge ${tone}` }, text); }

const STATE_TONE = {
  completed: "good", live: "good", clean: "good", ok: "good", PUSHED: "good", active: "good", approved: "good",
  auto_approved: "good", rolled_back: "neutral", ROLLED_BACK: "neutral", SKIPPED: "neutral", draft: "neutral",
  running: "info", rolling_back: "info", PUSHING: "info", QUEUED: "info", scanning: "info", pending: "info",
  awaiting_approval: "warn", paused: "warn", RETRY_WAIT: "warn", degraded: "warn", partially_completed: "warn",
  reauth_required: "warn", unknown: "neutral", receiving: "info", received: "info", imported: "good",
  failed: "bad", FAILED: "bad", DEAD_LETTER: "bad", quarantined: "bad", rejected: "bad", cancelled: "neutral",
  high: "bad", medium: "warn", low: "good",
};
const STATE_LABEL = {
  RETRY_WAIT: "Waiting to retry", DEAD_LETTER: "Needs attention", PUSHED: "Published", PUSHING: "Publishing",
  QUEUED: "Queued", FAILED: "Failed", SKIPPED: "Skipped", ROLLED_BACK: "Rolled back", VALIDATED: "Ready",
  CANCELLED: "Cancelled", DELISTED: "Delisted", awaiting_approval: "Awaiting approval",
  partially_completed: "Partly completed", rolling_back: "Rolling back", rolled_back: "Rolled back",
  reauth_required: "Reconnect needed", auto_approved: "Auto-approved",
};
export function stateBadge(state) {
  const label = STATE_LABEL[state] || String(state).replace(/_/g, " ");
  return badge(label.charAt(0).toUpperCase() + label.slice(1), STATE_TONE[state] || "neutral");
}

export function money(minor, currency) {
  if (minor === null || minor === undefined) return "—";
  try {
    const digits = new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions().maximumFractionDigits;
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(minor / 10 ** digits);
  } catch { return `${minor} ${currency || ""}`; }
}

export function when(iso) {
  if (!iso) return "—";
  const date = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  const seconds = (Date.now() - date.getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function show(value) {
  if (value === null || value === undefined || value === "") return "(empty)";
  if (typeof value === "string") return value;
  if (Array.isArray(value) && value.every((v) => v && typeof v === "object" && "key" in v)) {
    return value.map((v) => `${v.key}: ${v.value}`).join(", ");
  }
  return JSON.stringify(value);
}

export function emptyState(title, text, button) {
  return h("div", { class: "empty" }, h("h3", {}, title), h("p", {}, text), button || null);
}

export function table(columns, rows, { empty = "Nothing here yet." } = {}) {
  if (!rows.length) return h("p", { class: "muted pad" }, empty);
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.class || "" }, c.label)))),
    h("tbody", {}, rows.map((row) => h("tr", {}, columns.map((c) => h("td", { class: c.class || "" }, c.render(row))))))));
}
