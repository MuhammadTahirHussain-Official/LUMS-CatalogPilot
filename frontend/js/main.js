import * as api from "./api.js";
import { mountPip, pipOnRoute, unmountPip } from "./pip.js";
import { action, clear, confirmDialog, emptyState, field, h, icon, modal, showError, stateBadge, table, toast, when } from "./ui.js";
import { migrateView } from "./views-migrate.js";
import { catalogView, reviewView, uploadsView } from "./views-catalog.js";
import { activityView, channelsView, jobsView } from "./views-publish.js";

export const state = { me: null, catalogId: localStorage.getItem("cp.catalog") || null };
const root = document.getElementById("app");

export const can = (permission) => state.me?.permissions.includes(permission);
export function setCatalog(id) {
  state.catalogId = id;
  if (id) localStorage.setItem("cp.catalog", id); else localStorage.removeItem("cp.catalog");
}

// ------------------------------------------------------------------ session

async function loadMe() {
  try {
    state.me = await api.get("/me");
    api.setCsrf(state.me.csrf_token);
  } catch (error) {
    state.me = null;
    if (error.status !== 401) throw error;
  }
}

function stepUpDialog() {
  return new Promise((resolve) => {
    const mfa = state.me?.user.mfa_enabled;
    const input = h("input", mfa
      ? { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6", required: true }
      : { type: "password", autocomplete: "current-password", required: true });
    let done = false;
    const finish = (ok) => { if (!done) { done = true; m.close(); resolve(ok); } };
    const form = h("form", { onsubmit: action(async (event) => {
      event.preventDefault();
      await api.post("/auth/step-up", mfa ? { code: input.value.trim() } : { password: input.value });
      finish(true);
    }) },
    h("p", {}, "This is a sensitive action. Confirm it is you to continue."),
    field(mfa ? "Code from your authenticator app" : "Your password", input),
    h("div", { class: "row end" },
      h("button", { type: "button", onclick: () => finish(false) }, "Cancel"),
      h("button", { class: "primary", type: "submit" }, "Confirm")));
    const m = modal("Confirm it's you", form);
    m.dialog.addEventListener("cancel", () => finish(false));
    input.focus();
  });
}

api.setHandlers({
  stepUp: stepUpDialog,
  signedOut: () => { if (state.me) { state.me = null; toast("Your session ended. Sign in again."); render(); } },
});

// ------------------------------------------------------------------ auth screens

function authScreen() {
  let mode = "login";
  const box = h("div", { class: "auth-card" });
  const draw = () => {
    clear(box);
    const email = h("input", { type: "email", autocomplete: "email", required: true });
    const password = h("input", { type: "password", required: true, minlength: mode === "register" ? "12" : null,
      autocomplete: mode === "register" ? "new-password" : "current-password" });
    const org = h("input", { type: "text", maxlength: "200", placeholder: "e.g. Northwind Goods" });
    const submit = action(async (event) => {
      event.preventDefault();
      if (mode === "register") {
        const result = await api.post("/auth/register", { email: email.value, password: password.value, organization: org.value });
        if (result.dev_verification_token) {
          await api.post("/auth/verify-email", { token: result.dev_verification_token });
        } else {
          toast("Check your email for a verification code, then sign in.");
          mode = "login"; draw(); return;
        }
      }
      const login = await api.post("/auth/login", { email: email.value, password: password.value });
      api.setCsrf(login.csrf_token);
      if (login.mfa_required) { mode = "mfa"; draw(); return; }
      await loadMe(); render();
    });
    if (mode === "mfa") {
      const code = h("input", { type: "text", autocomplete: "one-time-code", required: true, maxlength: "20" });
      box.append(h("h1", {}, "Two-step verification"),
        h("form", { onsubmit: action(async (event) => {
          event.preventDefault();
          await api.post("/auth/mfa/verify", { code: code.value.trim() });
          await loadMe(); render();
        }) },
        field("Code from your authenticator app", code, "Lost your device? Enter one of your recovery codes instead."),
        h("button", { class: "primary block", type: "submit" }, "Verify")));
      code.focus();
      return;
    }
    box.append(
      h("div", { class: "brand" }, h("span", { class: "logo", "aria-hidden": "true" }, "◆"), "CatalogPilot"),
      h("h1", {}, mode === "login" ? "Sign in" : "Create your account"),
      h("p", { class: "muted" }, mode === "login"
        ? "One trusted home for your catalog."
        : "Bring your catalog in, clean it up, and publish it everywhere, safely."),
      h("form", { onsubmit: submit },
        field("Email", email),
        field("Password", password, mode === "register" ? "At least 12 characters. A passphrase works well." : null),
        mode === "register" ? field("Organization name", org) : null,
        h("button", { class: "primary block", type: "submit" }, mode === "login" ? "Sign in" : "Create account")),
      h("p", { class: "switch" }, mode === "login" ? "New here? " : "Already have an account? ",
        h("button", { class: "link", type: "button", onclick: () => { mode = mode === "login" ? "register" : "login"; draw(); } },
          mode === "login" ? "Create an account" : "Sign in")));
    email.focus();
  };
  draw();
  return h("main", { class: "auth" }, box);
}

// ------------------------------------------------------------------ shell and routing

const NAV = [
  ["overview", "Home", null, "home"],
  ["migrate", "Move a store", "jobs:write", "move"],
  ["catalog", "Products", "catalog:read", "box"],
  ["uploads", "Upload a file", "uploads:write", "upload"],
  ["review", "Review suggestions", "catalog:read", "check"],
  ["channels", "Stores", "channels:read", "plug"],
  ["jobs", "Publishing", "jobs:read", "send"],
  ["activity", "Activity log", "audit:read", "clock"],
  ["settings", "Settings", null, "gear"],
];
const VIEWS = { overview: overviewView, migrate: migrateView, catalog: catalogView, uploads: uploadsView, review: reviewView,
  channels: channelsView, jobs: jobsView, activity: activityView, settings: settingsView };

let teardown = null;
export function navigate(route) { location.hash = `#/${route}`; }

async function render() {
  if (teardown) { try { teardown(); } catch { /* view already gone */ } teardown = null; }
  if (!state.me || state.me.mfa_pending) { unmountPip(); clear(root).append(authScreen()); return; }
  const [route, ...params] = (location.hash.replace(/^#\//, "") || "overview").split("/");
  const name = VIEWS[route] ? route : "overview";
  const org = state.me.organizations.find((o) => o.id === state.me.organization_id);
  const content = h("section", { class: "content", tabindex: "-1" }, h("p", { class: "muted pad" }, "Loading…"));
  clear(root).append(h("div", { class: "shell" },
    h("nav", { class: "side", "aria-label": "Main" },
      h("div", { class: "brand" }, h("span", { class: "logo", "aria-hidden": "true" }, "◆"), "CatalogPilot"),
      h("ul", {}, NAV.filter(([, , perm]) => !perm || can(perm)).map(([key, label, , glyph]) =>
        h("li", {}, h("a", { href: `#/${key}`, class: key === name ? "on" : "", "aria-current": key === name ? "page" : null }, icon(glyph), h("span", {}, label))))),
      h("div", { class: "who" },
        h("div", { class: "org" }, org?.name || "Organization"),
        h("div", { class: "muted small" }, `${state.me.user.email} · ${state.me.role}`),
        h("button", { class: "link", onclick: action(async () => { await api.post("/auth/logout"); state.me = null; render(); }) }, "Sign out"))),
    h("div", { class: "main" },
      h("div", { id: "offline-banner", class: "banner warn", role: "status", hidden: navigator.onLine },
        "You are offline. Work already running continues on the server; this page will catch up when you reconnect."),
      org?.frozen ? h("div", { class: "banner bad", role: "alert" }, "Publishing is frozen for this organization. Nothing will be sent to any channel until an owner unfreezes it in Settings.") : null,
      content)));
  mountPip();
  const seenKey = `cp.seen.${name}`;
  const firstVisit = !localStorage.getItem(seenKey);
  localStorage.setItem(seenKey, "1");
  pipOnRoute(name, pipNext, firstVisit);
  try {
    const result = await VIEWS[name](content, params);
    if (typeof result === "function") teardown = result;
  } catch (error) {
    clear(content).append(emptyState("This page could not be loaded", error.message,
      h("button", { onclick: render }, "Try again")));
  }
}

export const refresh = render;
window.addEventListener("hashchange", render);
// Connection changes only toggle a banner. Re-rendering here would throw away whatever the
// person was typing when their connection flickered.
const setOffline = (offline) => { const b = document.getElementById("offline-banner"); if (b) b.hidden = !offline; };
window.addEventListener("online", () => setOffline(false));
window.addEventListener("offline", () => setOffline(true));
let pipNext = { label: "move your first store", route: "migrate" };

// ------------------------------------------------------------------ overview

async function overviewView(el) {
  const [catalogs, channels, jobs, proposals] = await Promise.all([
    can("catalog:read") ? api.get("/catalogs") : { data: [] },
    can("channels:read") ? api.get("/channels") : { data: [] },
    can("jobs:read") ? api.get("/jobs?limit=5") : { data: [] },
    can("catalog:read") ? api.get("/proposals?limit=1") : { pending: {} },
  ]);
  const products = catalogs.data.reduce((sum, c) => sum + (c.products || 0), 0);
  const pending = Object.values(proposals.pending || {}).reduce((a, b) => a + b, 0);
  const connected = channels.data.filter((c) => c.connected).length;
  const published = jobs.data.some((j) => j.kind === "publish" && j.state === "completed");
  const steps = [
    ["Connect the store you are moving from", connected > 0, "migrate"],
    ["Bring your products in", products > 0, "migrate"],
    ["Review suggested improvements", products > 0 && pending === 0, pending ? "review" : "migrate"],
    ["Connect the store you are moving to", connected > 1, "migrate"],
    ["Preview and publish", published, "migrate"],
  ];
  const next = steps.find(([, done]) => !done);
  pipNext = next ? { label: next[0].charAt(0).toLowerCase() + next[0].slice(1), route: next[2] } : null;
  pipOnRoute("overview", pipNext, false);
  clear(el).append(
    h("header", { class: "page" }, h("div", {}, h("h1", {}, `Welcome${state.me.user.name ? `, ${state.me.user.name}` : ""}`),
      h("p", { class: "muted" }, "Move your products from one store to another, safely.")),
      can("jobs:write") ? h("a", { class: "button primary big", href: "#/migrate" }, icon("move"), products ? "Continue moving a store" : "Move a store") : null),
    h("div", { class: "stats" },
      stat("Products", products, "catalog"), stat("Suggestions to review", pending, "review"),
      stat("Connected stores", connected, "channels"),
      stat("Jobs needing attention", jobs.data.filter((j) => ["awaiting_approval", "failed", "partially_completed", "paused"].includes(j.state)).length, "jobs")),
    h("div", { class: "grid2" },
      h("section", { class: "card" }, h("h2", {}, "Getting started"),
        h("ol", { class: "steps" }, steps.map(([label, done, route]) =>
          h("li", { class: done ? "done" : "" }, h("span", { class: "tick", "aria-hidden": "true" }, done ? "✓" : ""),
            h("a", { href: `#/${route}` }, label), done ? h("span", { class: "sr" }, " (done)") : null))),
        next ? h("a", { class: "button primary", href: `#/${next[2]}` }, `Next: ${next[0]}`) : h("p", { class: "muted" }, "You're all set.")),
      h("section", { class: "card" }, h("h2", {}, "Recent publishing"),
        table([
          { label: "Job", render: (j) => h("a", { href: `#/jobs/${j.id}` }, j.kind === "rollback" ? "Rollback" : "Publish") },
          { label: "Status", render: (j) => stateBadge(j.state) },
          { label: "Started", render: (j) => when(j.created_at) },
        ], jobs.data, { empty: "No publishing jobs yet." }))));
}

function stat(label, value, route) {
  return h("a", { class: "stat", href: `#/${route}` }, h("div", { class: "num" }, String(value)), h("div", { class: "muted" }, label));
}

// ------------------------------------------------------------------ settings

async function settingsView(el) {
  const policy = await api.get("/org/policies");
  const org = state.me.organizations.find((o) => o.id === state.me.organization_id);
  const sections = [];

  if (can("policy:admin")) {
    const mode = h("select", {}, [["manual", "Manual: I approve every change"], ["assisted", "Assisted: apply safe, high-confidence changes automatically"]]
      .map(([v, label]) => h("option", { value: v, selected: policy.ai_mode === v || (v === "assisted" && policy.ai_mode === "automatic") }, label)));
    const enabled = h("input", { type: "checkbox", checked: policy.ai_enabled });
    const external = h("input", { type: "checkbox", checked: policy.external_ai_allowed });
    const fields = ["title", "description_html", "attributes", "category"].map((f) =>
      [f, h("input", { type: "checkbox", checked: policy.ai_field_allowlist.includes(f) })]);
    sections.push(h("section", { class: "card" }, h("h2", {}, "AI assistance"),
      h("label", { class: "check" }, enabled, "Suggest improvements to my products"),
      field("How suggestions are applied", mode, "Price, stock and category changes always need your approval."),
      h("fieldset", {}, h("legend", {}, "Fields AI may suggest changes to"),
        fields.map(([f, box]) => h("label", { class: "check" }, box, { title: "Titles", description_html: "Descriptions", attributes: "Attributes", category: "Categories" }[f]))),
      h("label", { class: "check" }, external, "Allow an external AI provider to process product text"),
      h("small", { class: "muted" }, "Off by default. When off, all processing stays on this server."),
      h("div", { class: "row end" }, h("button", { class: "primary", onclick: action(async () => {
        await api.put("/org/policies", { ai_enabled: enabled.checked, ai_mode: mode.value, external_ai_allowed: external.checked,
          ai_field_allowlist: fields.filter(([, box]) => box.checked).map(([f]) => f) });
        toast("Settings saved.", "good");
      }) }, "Save"))));
  }

  const security = h("section", { class: "card" }, h("h2", {}, "Sign-in security"));
  if (state.me.user.mfa_enabled) {
    security.append(h("p", {}, stateBadge("active"), " Two-step verification is on."));
  } else {
    security.append(h("p", { class: "muted" }, "Add a second step when signing in. Recommended for everyone, required to manage channels safely."),
      h("button", { onclick: action(async () => {
        const setup = await api.post("/auth/mfa/setup");
        const code = h("input", { type: "text", inputmode: "numeric", maxlength: "6", required: true, autocomplete: "one-time-code" });
        const m = modal("Turn on two-step verification", h("form", { onsubmit: action(async (event) => {
          event.preventDefault();
          const result = await api.post("/auth/mfa/enable", { code: code.value.trim() });
          m.close();
          modal("Save your recovery codes", h("div", {},
            h("p", {}, "Each code works once if you lose your device. They are shown only now."),
            h("pre", { class: "codes" }, result.recovery_codes.join("\n"))));
          await loadMe(); render();
        }) },
        h("p", {}, "Add this key to an authenticator app, then enter the 6-digit code it shows."),
        h("pre", { class: "codes" }, setup.secret),
        field("6-digit code", code),
        h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Turn on"))));
      }) }, "Set up two-step verification"));
  }
  const sessions = await api.get("/me/sessions");
  security.append(h("h3", {}, "Signed-in devices"), table([
    { label: "Device", render: (s) => (s.user_agent || "Unknown").slice(0, 60) },
    { label: "Address", render: (s) => s.ip || "—" },
    { label: "Last active", render: (s) => (s.current ? "This device" : when(s.last_seen_at)) },
  ], sessions.data), sessions.data.length > 1 ? h("button", { onclick: action(async () => {
    await api.del("/me/sessions/others"); toast("Other devices signed out.", "good"); render();
  }) }, "Sign out other devices") : null);
  sections.push(security);

  if (can("members:admin")) {
    const members = await api.get("/org/members");
    const email = h("input", { type: "email", required: true, placeholder: "name@company.com" });
    const role = h("select", {}, ["viewer", "reviewer", "editor", "publisher", "admin"].map((r) => h("option", { value: r }, r)));
    sections.push(h("section", { class: "card" }, h("h2", {}, "People"),
      table([
        { label: "Email", render: (m) => m.email },
        { label: "Role", render: (m) => m.role },
        { label: "Two-step", render: (m) => (m.mfa ? "On" : "Off") },
        { label: "", class: "right", render: (m) => (m.user_id === state.me.user.id ? "" : h("button", { class: "link danger", onclick: action(async () => {
          if (!await confirmDialog("Remove this person?", `${m.email} will lose access immediately. Channels they connected keep working.`, "Remove", true)) return;
          await api.del(`/org/members/${m.user_id}`); render();
        }) }, "Remove")) },
      ], members.data),
      h("form", { class: "row", onsubmit: action(async (event) => {
        event.preventDefault();
        const result = await api.post("/org/invitations", { email: email.value, role: role.value });
        modal("Invitation created", h("div", {}, h("p", {}, `Send this code to ${result.email}. They create an account, then enter it under Settings → Join an organization.`),
          h("pre", { class: "codes" }, result.dev_invitation_token || "Sent by email.")));
        email.value = "";
      }) }, email, role, h("button", { type: "submit" }, "Invite"))));
  }

  const join = h("input", { type: "text", placeholder: "Invitation code" });
  sections.push(h("section", { class: "card" }, h("h2", {}, "Join an organization"),
    h("div", { class: "row" }, join, h("button", { onclick: action(async () => {
      await api.post("/invitations/accept", { token: join.value.trim() });
      await loadMe(); toast("You joined the organization.", "good"); navigate("overview"); render();
    }) }, "Join"))));

  if (can("org:owner")) {
    sections.push(h("section", { class: "card danger-zone" }, h("h2", {}, "Emergency stop"),
      h("p", { class: "muted" }, "Freezing stops all publishing to every channel at once. Use it if something looks wrong."),
      org?.frozen
        ? h("button", { onclick: action(async () => { await api.post("/org/unfreeze"); await loadMe(); render(); }) }, "Unfreeze publishing")
        : h("button", { class: "danger", onclick: action(async () => {
          if (!await confirmDialog("Freeze all publishing?", "Running jobs pause and nothing is sent to any channel until you unfreeze.", "Freeze", true)) return;
          await api.post("/org/freeze"); await loadMe(); render();
        }) }, "Freeze all publishing")));
  }
  clear(el).append(h("header", { class: "page" }, h("h1", {}, "Settings")), h("div", { class: "stack" }, sections));
}

// ------------------------------------------------------------------ boot

(async () => {
  try { await loadMe(); } catch (error) { showError(error); }
  render();
})();
