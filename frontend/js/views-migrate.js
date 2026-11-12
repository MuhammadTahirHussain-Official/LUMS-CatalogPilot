// "Move a store": one guided path from a source store to a destination store.
// Connect source -> bring products in -> improve them -> connect destination -> preview & publish.
// Progress is remembered per organization, so closing the tab or losing the connection
// never loses your place; long-running work continues on the server meanwhile.

import * as api from "./api.js";
import { can, navigate, setCatalog, state } from "./main.js";
import { pipSay } from "./pip.js";
import { action, badge, clear, emptyState, field, h, showError, stateBadge, toast } from "./ui.js";

const STEPS = ["Your current store", "Bring products in", "Improve them", "Your new store", "Preview & publish"];
const PLATFORM_HELP = {
  mock: "A practice store that comes filled with 1,200 sample products. Perfect for a first try: nothing real is touched.",
  shopify: "In Shopify admin: Settings → Apps → Develop apps → create an app with product read/write access, then copy its Admin API access token.",
  woocommerce: "In WordPress: WooCommerce → Settings → Advanced → REST API → Add key with Read/Write permission.",
  csv_feed: "No store yet? Publish to a product feed file you can download and import anywhere.",
};
const KEY_LABEL = { access_token: "Access token", consumer_key: "Consumer key", consumer_secret: "Consumer secret" };

const storeKey = () => `cp.migration.${state.me.organization_id}`;
function load() {
  try { return { step: 0, ...JSON.parse(localStorage.getItem(storeKey()) || "{}") }; } catch { return { step: 0 }; }
}
function save(m) { localStorage.setItem(storeKey(), JSON.stringify(m)); }

export async function migrateView(el) {
  if (!can("jobs:write") || !can("channels:read")) {
    clear(el).append(h("header", { class: "page" }, h("h1", {}, "Move a store")),
      emptyState("You need a different role for this", "Moving a store needs permission to publish. Ask an owner or admin of your organization."));
    return undefined;
  }
  let m = load();
  let timer = null;
  let alive = true;
  const body = h("div", { class: "wizard-body" });
  const stepper = h("ol", { class: "stepper" });

  const [platformList, catalogList] = await Promise.all([api.get("/platforms"), api.get("/catalogs")]);
  const platforms = Object.fromEntries(platformList.data.map((p) => [p.platform, p]));
  let catalogs = catalogList.data;
  if (!catalogs.some((c) => c.id === state.catalogId)) setCatalog(catalogs[0]?.id || null);

  const go = (step) => { m.step = Math.max(0, Math.min(STEPS.length - 1, step)); save(m); draw(); };
  const reached = () => (m.publishJobId ? 4 : m.destId ? 4 : m.improved ? 3 : m.importDone ? 2 : m.sourceId ? 1 : 0);

  function drawStepper() {
    clear(stepper).append(STEPS.map((label, i) => {
      const status = i < m.step ? "done" : i === m.step ? "now" : "todo";
      const allowed = i <= reached();
      return h("li", { class: status, "aria-current": status === "now" ? "step" : null },
        h("button", { class: "step", disabled: !allowed, onclick: () => go(i) },
          h("span", { class: "dot", "aria-hidden": "true" }, status === "done" ? "✓" : String(i + 1)),
          h("span", { class: "label" }, label, h("span", { class: "sr" }, status === "done" ? " (done)" : ""))));
    }));
  }

  async function ensureCatalog() {
    if (state.catalogId) return state.catalogId;
    const created = await api.post("/catalogs", { name: "My catalog" });
    catalogs = [created];
    setCatalog(created.id);
    return created.id;
  }

  // ---- shared: choose or connect a store ---------------------------------------------
  async function storePicker(role, chosenId, exclude, onChosen) {
    const channels = (await api.get("/channels")).data;
    const usable = channels.filter((c) => c.connected && c.id !== exclude && (role === "source" ? c.can_import : c.role !== "source"));
    const box = h("div", { class: "stack" });
    if (usable.length) {
      box.append(h("div", { class: "choice-grid" }, usable.map((c) =>
        h("button", { class: `choice ${c.id === chosenId ? "on" : ""}`, onclick: () => onChosen(c.id) },
          h("strong", {}, c.name), h("span", { class: "muted small" }, platforms[c.platform]?.label || ""),
          c.health !== "ok" ? stateBadge(c.health) : null))));
    }
    if (!can("channels:admin")) {
      if (!usable.length) box.append(h("p", { class: "muted" }, "No store is connected yet. Ask an owner or admin to connect one under Channels."));
      return box;
    }
    const options = platformList.data.filter((p) => (role === "source" ? p.can_import : true));
    let pending = null; // a store created but not yet connected (wrong key): reused on the next try
    const form = h("div", { class: "card connect" });
    const drawForm = (platform) => {
      const p = platforms[platform];
      const name = h("input", { type: "text", maxlength: "200", required: true, value: role === "source" ? `My ${p.label}` : `New ${p.label}` });
      const url = h("input", { type: "url", placeholder: "https://your-store.example.com", required: p.needs_base_url });
      const keys = p.credential_fields.map((f) => [f, h("input", { type: "password", autocomplete: "off", spellcheck: "false", required: true, maxlength: "8192",
        placeholder: platform === "mock" ? "any 12+ characters, e.g. practice-key-123" : "" })]);
      clear(form).append(
        h("div", { class: "segmented", role: "tablist" }, options.map((o) =>
          h("button", { role: "tab", "aria-selected": String(o.platform === platform), class: o.platform === platform ? "on" : "", onclick: () => { pending = null; drawForm(o.platform); } },
            o.platform === "mock" ? "Practice store" : o.label, o.beta ? badge("Beta", "info") : null))),
        h("p", { class: "muted" }, PLATFORM_HELP[platform] || ""),
        h("form", { autocomplete: "off", onsubmit: action(async (event) => {
          event.preventDefault();
          if (!pending) {
            pending = await api.post("/channels", { platform, name: name.value, role: role === "source" ? "source" : "destination",
              base_url: p.needs_base_url ? url.value.trim() : null });
          }
          if (pending.needs_credential) {
            await api.put(`/channels/${pending.id}/credential`, { kind: p.credential_kinds[0],
              secret: Object.fromEntries(keys.map(([f, input]) => [f, input.value.trim()])) });
            keys.forEach(([, input]) => { input.value = ""; });
          }
          toast(`${name.value} is connected.`, "good");
          onChosen(pending.id);
        }) },
        field("Name it", name), p.needs_base_url ? field("Store address", url, "Starts with https://") : null,
        keys.map(([f, input]) => field(KEY_LABEL[f] || f, input)),
        p.credential_fields.length ? h("p", { class: "note" }, "Use a key made for apps, never your login password. It is checked, encrypted and can never be displayed again.") : null,
        h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Connect"))));
    };
    drawForm(options[0].platform);
    box.append(h("h3", {}, usable.length ? "Or connect another store" : "Connect a store"), form);
    return box;
  }

  // ---- step 1: source ------------------------------------------------------------------
  async function stepSource() {
    const picker = await storePicker("source", m.sourceId, m.destId, (id) => {
      if (id !== m.sourceId) m = { ...m, sourceId: id, importJobId: null, importDone: false };
      go(1);
    });
    clear(body).append(h("h2", {}, "Which store are you moving from?"),
      h("p", { class: "muted" }, "We only read from this store. Nothing in it is changed or removed."), picker);
  }

  // ---- step 2: import ------------------------------------------------------------------
  async function stepImport() {
    const catalogId = await ensureCatalog();
    const view = h("div", { class: "stack" });
    clear(body).append(h("h2", {}, "Bring your products in"),
      h("p", { class: "muted" }, "We copy your products into your catalog here, where you can check and improve them. Safe to repeat: products are matched, never duplicated."), view);

    const drawJob = (job) => {
      const r = job.preview || {};
      const item = job.counts;
      const waiting = (item.RETRY_WAIT || 0) > 0;
      const running = ["running", "paused"].includes(job.state);
      const rows = [["Found in your store", r.seen ?? 0], ["New here", r.created ?? 0], ["Updated", r.updated ?? 0],
        ["Already up to date", r.unchanged ?? 0], ["Could not be read", r.skipped ?? 0]];
      const q = r.quality || {};
      clear(view).append(
        h("div", { class: `status-line ${job.state}` }, stateBadge(waiting ? "paused" : job.state),
          running ? h("span", {}, waiting ? " The store is slow to answer. Retrying automatically…" : ` Reading page ${(r.pages || 0) + 1}…`) : null,
          running ? h("progress", { "aria-label": "Import in progress" }) : null),
        h("div", { class: "stats" }, rows.map(([label, n]) => h("div", { class: "stat" }, h("div", { class: "num" }, Number(n).toLocaleString()), h("div", { class: "muted" }, label)))),
        job.state === "completed" && Object.keys(q).length ? h("div", { class: "card" }, h("h3", {}, "Things worth knowing before you publish"),
          h("ul", {}, q.missing_images ? h("li", {}, `${q.missing_images} product(s) have no image`) : null,
            q.missing_brand ? h("li", {}, `${q.missing_brand} have no brand`) : null,
            q.missing_category ? h("li", {}, `${q.missing_category} have no category (the next step can suggest one)`) : null,
            q.missing_price ? h("li", {}, `${q.missing_price} have no price and will be held back from publishing`) : null)) : null,
        (r.problems || []).length ? h("details", { class: "card" }, h("summary", {}, `${r.skipped} product(s) could not be read. See why`),
          h("ul", { class: "small" }, r.problems.slice(0, 50).map((p) => h("li", {}, p.reason)))) : null,
        job.state === "failed" ? h("div", { class: "banner bad" }, "The import stopped before the end. Everything read so far is saved. ",
          h("button", { onclick: action(async () => { await api.post(`/jobs/${job.id}/items/retry`, {}); poll(); }) }, "Continue from where it stopped")) : null,
        h("div", { class: "row end" },
          running ? h("button", { onclick: action(async () => { await api.post(`/jobs/${job.id}/control/cancel`); poll(); }) }, "Stop") : null,
          !running ? h("button", { onclick: action(start) }, "Read the store again") : null,
          job.state === "completed" ? h("button", { class: "primary", onclick: () => go(2) }, "Next: improve them") : null));
    };

    async function poll() {
      clearTimeout(timer);
      if (!alive || m.step !== 1 || !m.importJobId) return;
      try {
        const job = await api.get(`/jobs/${m.importJobId}`);
        drawJob(job);
        if (job.state === "completed" && !m.importDone) {
          m.importDone = true; save(m); drawStepper();
          pipSay(`All done! ${(job.preview?.created || 0) + (job.preview?.unchanged || 0) + (job.preview?.updated || 0)} products are safely in your catalog.`, "cheer");
        }
        if (["running", "paused"].includes(job.state)) timer = setTimeout(poll, 1500);
      } catch (error) {
        if (error.status === 404) { m.importJobId = null; m.importDone = false; save(m); stepImport(); return; }
        timer = setTimeout(poll, 4000); // offline or a blip: the import itself carries on server-side
      }
    }

    async function start() {
      try {
        const job = await api.post("/jobs", { kind: "import", catalog_id: catalogId, channel_ids: [m.sourceId] }, { "Idempotency-Key": crypto.randomUUID() });
        m.importJobId = job.id;
      } catch (error) {
        if (error.code !== "job_already_running" || !error.problem.job_id) throw error;
        m.importJobId = error.problem.job_id; // an import from this store is already under way: show it
      }
      m.importDone = false; save(m); drawStepper();
      await poll();
    }

    if (m.importJobId) { await poll(); return; }
    clear(view).append(h("div", { class: "card center" },
      h("p", {}, "Ready when you are. Large stores take a few minutes; you can close this page and come back."),
      h("button", { class: "primary big", onclick: action(start) }, "Bring my products in")));
  }

  // ---- step 3: improve -----------------------------------------------------------------
  async function stepImprove() {
    const catalogId = await ensureCatalog();
    const result = h("div", {});
    const style = ["title", "sentence", "keep"].map((v, i) => [v, h("input", { type: "radio", name: "style", value: v, checked: i === 0 })]);
    const promo = h("input", { type: "checkbox", checked: true });
    const brand = h("input", { type: "checkbox" });
    const limit = h("select", {}, [["", "Leave title length alone"], ["70", "Up to 70 characters (marketplaces)"], ["120", "Up to 120 characters"], ["150", "Up to 150 characters"]]
      .map(([v, l]) => h("option", { value: v }, l)));
    const what = [["title", "Tidy titles", true], ["description", "Clean descriptions (remove pasted links and clutter)", true],
      ["attributes", "Find colour, material and size", true], ["category", "Suggest a category", true], ["seo", "Write search-result text where it is missing", true]]
      .map(([v, l, on]) => [v, l, h("input", { type: "checkbox", checked: on })]);

    const pendingCount = async () => Object.values((await api.get("/proposals?limit=1")).pending || {}).reduce((a, b) => a + b, 0);
    const drawResult = async (summary) => {
      const pending = await pendingCount();
      clear(result).append(h("div", { class: "card" },
        summary ? h("p", {}, h("strong", {}, `${summary.proposals.toLocaleString()} suggestion(s). `),
          `${summary.auto_approved.toLocaleString()} safe ones were applied automatically; ${summary.pending.toLocaleString()} are waiting for you.`) : null,
        pending ? h("p", {}, `${pending.toLocaleString()} suggestion(s) are waiting for your decision.`) : h("p", { class: "muted" }, "Nothing is waiting for review."),
        h("div", { class: "row" },
          pending ? h("a", { class: "button", href: "#/review" }, "Look through them") : null,
          pending && can("reviews:write") ? h("button", { onclick: action(async () => {
            let applied = 0;
            for (let round = 0; round < 60; round += 1) {
              const page = await api.get("/proposals?status=pending&risk=low&limit=200");
              if (!page.data.length) break;
              const r = await api.post("/proposals:decide", { proposal_ids: page.data.map((p) => p.id), decision: "approve" });
              applied += r.applied;
              if (!r.applied && !r.superseded) break;
            }
            toast(`${applied.toLocaleString()} improvement(s) applied. Higher-impact ones still wait for you.`, "good");
            await drawResult(null);
          }) }, "Approve all low-impact suggestions") : null,
          h("button", { class: "primary", onclick: () => go(3) }, "Next: your new store"))));
      result.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };

    clear(body).append(h("h2", {}, "How should we improve your products?"),
      h("p", { class: "muted" }, "Answer a few questions and we'll suggest changes. Nothing changes until it is approved, prices and stock are never touched, and every change can be undone."),
      h("form", { class: "card questions", onsubmit: action(async (event) => {
        event.preventDefault();
        const tasks = what.filter(([, , box]) => box.checked).map(([v]) => v);
        if (!tasks.length) { toast("Pick at least one thing to improve, or skip this step.", "error"); return; }
        pipSay("Reading through your products. This can take a minute for a big catalog…", "thinking", 20000);
        const summary = await api.post(`/catalogs/${catalogId}/enhance`, { tasks, preferences: {
          title_style: style.find(([, r]) => r.checked)[0], remove_promo: promo.checked, brand_first: brand.checked,
          max_title_length: limit.value ? Number(limit.value) : null } });
        m.improved = true; save(m); drawStepper();
        await drawResult(summary);
      }) },
      h("fieldset", {}, h("legend", {}, "What would you like help with?"), what.map(([, label, box]) => h("label", { class: "check" }, box, label))),
      h("fieldset", {}, h("legend", {}, "How should titles look?"),
        [["Title Case Like This", 0], ["Sentence case like this", 1], ["Keep my capitalization", 2]].map(([label, i]) => h("label", { class: "check" }, style[i][1], label))),
      h("label", { class: "check" }, promo, "Remove sales phrases such as “FREE SHIPPING!!!” and “BEST PRICE”"),
      h("label", { class: "check" }, brand, "Put the brand name at the start of each title"),
      field("Title length", limit),
      h("div", { class: "row end" },
        h("button", { type: "button", onclick: () => { m.improved = true; save(m); go(3); } }, "Skip this step"),
        h("button", { class: "primary", type: "submit" }, "Suggest improvements"))),
      result);
    if (m.improved) await drawResult(null);
  }

  // ---- step 4: destination -------------------------------------------------------------
  async function stepDestination() {
    const picker = await storePicker("destination", m.destId, m.sourceId, (id) => { m.destId = id; m.publishJobId = null; go(4); });
    clear(body).append(h("h2", {}, "Where are your products going?"),
      h("p", { class: "muted" }, "Connecting a store does not publish anything. You'll see a full preview first."), picker);
  }

  // ---- step 5: publish -----------------------------------------------------------------
  async function stepPublish() {
    const catalogId = await ensureCatalog();
    const counts = await Promise.all(["active", "draft"].map((s) => api.get(`/catalogs/${catalogId}/products?status=${s}&limit=1`)));
    const hasActive = counts[0].data.length > 0;
    const trial = h("input", { type: "checkbox", checked: true });
    clear(body).append(h("h2", {}, "Preview, then publish"),
      h("p", { class: "muted" }, "First you get a preview listing every product and exactly what will be created. Nothing is sent until you approve it, and a finished publish can be rolled back."),
      !hasActive ? h("div", { class: "banner warn" }, "None of your products are marked Active yet, so there is nothing to publish. ",
        h("a", { href: "#/catalog" }, "Open the catalog"), ", select the products you want and choose “Mark active”.") : null,
      counts[1].data.length && hasActive ? h("p", { class: "note" }, "Products still marked Draft stay here and are not published.") : null,
      h("div", { class: "card" },
        h("label", { class: "check" }, trial, "Start with a trial of 10 products (recommended the first time)"),
        h("div", { class: "row end" },
          m.publishJobId ? h("a", { class: "button", href: `#/jobs/${m.publishJobId}` }, "Open my last publish") : null,
          h("button", { class: "primary big", disabled: !hasActive, onclick: action(async () => {
            const job = await api.post("/jobs", { kind: "publish", catalog_id: catalogId, channel_ids: [m.destId], options: { sandbox: trial.checked } },
              { "Idempotency-Key": crypto.randomUUID() });
            m.publishJobId = job.id; save(m);
            navigate(`jobs/${job.id}`);
          }) }, "Show me the preview"))),
      h("div", { class: "row end" }, h("button", { class: "link", onclick: () => { localStorage.removeItem(storeKey()); m = load(); draw(); } }, "Start a new move from the beginning")));
  }

  async function draw() {
    clearTimeout(timer);
    if (m.step > reached()) m.step = reached();
    drawStepper();
    clear(body).append(h("p", { class: "muted pad" }, "Loading…"));
    try {
      await [stepSource, stepImport, stepImprove, stepDestination, stepPublish][m.step]();
      if (m.step > 0) body.prepend(h("button", { class: "link back", onclick: () => go(m.step - 1) }, "← Back"));
    } catch (error) {
      showError(error);
      clear(body).append(emptyState("This step could not be loaded", "Check your connection and try again. Your progress is saved.", h("button", { onclick: draw }, "Try again")));
    }
  }

  clear(el).append(h("header", { class: "page" }, h("div", {}, h("h1", {}, "Move a store"),
    h("p", { class: "muted" }, "From your current store to your new one, in five guided steps."))), stepper, body);
  await draw();
  return () => { alive = false; clearTimeout(timer); };
}
