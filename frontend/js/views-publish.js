import * as api from "./api.js";
import { can, navigate, refresh, state } from "./main.js";
import { action, badge, clear, confirmDialog, emptyState, field, h, modal, show, showError, stateBadge, table, toast, when } from "./ui.js";

// ------------------------------------------------------------------ channels

export async function channelsView(el) {
  const [channels, platforms] = await Promise.all([api.get("/channels"), api.get("/platforms")]);
  const byPlatform = Object.fromEntries(platforms.data.map((p) => [p.platform, p]));
  const admin = can("channels:admin");

  const connect = (channel) => {
    const platform = byPlatform[channel.platform];
    const LABEL = { access_token: "Access token", consumer_key: "Consumer key", consumer_secret: "Consumer secret" };
    const inputs = platform.credential_fields.map((name) =>
      [name, h("input", { type: "password", required: true, autocomplete: "off", spellcheck: "false", maxlength: "8192" })]);
    const m = modal(`Connect ${channel.name}`, h("form", { autocomplete: "off", onsubmit: action(async (event) => {
      event.preventDefault();
      const meta = await api.put(`/channels/${channel.id}/credential`, {
        kind: platform.credential_kinds[0], secret: Object.fromEntries(inputs.map(([name, input]) => [name, input.value.trim()])) });
      inputs.forEach(([, input]) => { input.value = ""; });
      toast(`Connected. Key ending ${meta.hint.replace("…", "")} was verified and stored encrypted.`, "good");
      m.close(); refresh();
    }) },
    h("div", { class: "banner info" },
      h("strong", {}, "What we accept: "), "an API key or token created for this integration. ",
      h("strong", {}, "Never "), "your store login password, a verification code or card details."),
    channel.platform === "mock" ? h("p", { class: "muted" }, "The sandbox store accepts any token of 12+ characters, e.g. sandbox-token-123.") : null,
    inputs.map(([name, input]) => field(LABEL[name] || name, input)),
    h("p", { class: "muted small" }, "We test the key against the store before saving. After saving, nobody, including you and our staff, can view it again; you can only replace or remove it."),
    h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Verify and save"))));
    inputs[0]?.[1].focus();
  };

  const add = () => {
    const platform = h("select", {}, platforms.data.map((p) =>
      h("option", { value: p.platform }, p.verified ? p.label : `${p.label} (preview, not yet verified against live stores)`)));
    const name = h("input", { type: "text", required: true, maxlength: "200" });
    const url = h("input", { type: "url", placeholder: "https://your-store.example.com" });
    const urlField = field("Store URL", url, "Must start with https://");
    const sync = () => {
      const p = byPlatform[platform.value];
      urlField.hidden = !p.needs_base_url; url.required = p.needs_base_url;
      if (!name.value || platforms.data.some((x) => x.label === name.value)) name.value = p.label;
    };
    platform.addEventListener("change", sync); sync();
    const m = modal("Add a channel", h("form", { onsubmit: action(async (event) => {
      event.preventDefault();
      const created = await api.post("/channels", { platform: platform.value, name: name.value, base_url: urlField.hidden ? null : url.value });
      m.close();
      if (created.needs_credential) connect(created); else refresh();
    }) }, field("Where do you want to publish?", platform), field("Name", name), urlField,
    h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Add channel"))));
  };

  const cards = channels.data.map((c) => h("article", { class: "card channel" },
    h("div", { class: "row between" },
      h("div", {}, h("h2", {}, c.name), h("div", { class: "muted small" }, [byPlatform[c.platform]?.label, c.base_url].filter(Boolean).join(" · "))),
      stateBadge(c.connected ? c.health : "unknown")),
    !c.connected ? h("p", { class: "warn-text" }, "Not connected yet.") : null,
    c.health === "reauth_required" ? h("p", { class: "bad-text" }, "The store rejected our access. Reconnect with a new key; waiting items will resume.") : null,
    c.health === "degraded" ? h("p", { class: "warn-text" }, "The store is returning errors. Publishing to it is paused and will retry automatically.") : null,
    c.paused_reason && c.health === "paused" ? h("p", { class: "muted" }, `Paused: ${c.paused_reason}.`) : null,
    h("p", { class: "muted" }, `${c.live_listings} live listing(s)`),
    h("div", { class: "row" },
      admin && c.needs_credential ? h("button", { class: c.connected ? "" : "primary", onclick: () => connect(c) }, c.connected ? "Replace key" : "Connect") : null,
      c.platform === "csv_feed" ? h("button", { onclick: action(() => api.download("GET", `/channels/${c.id}/feed.csv`, "feed.csv")) }, "Download feed") : null,
      can("publish:write") && c.connected && c.health !== "paused" ? h("button", { onclick: action(async () => { await api.post(`/channels/${c.id}/pause`); refresh(); }) }, "Pause") : null,
      can("publish:write") && c.health === "paused" && c.paused_reason !== "disconnected" ? h("button", { onclick: action(async () => { await api.post(`/channels/${c.id}/resume`); refresh(); }) }, "Resume") : null,
      admin && c.connected && c.needs_credential ? h("button", { class: "link", onclick: action(async () => {
        const usage = await api.get(`/channels/${c.id}/credential/usage`);
        const meta = await api.get(`/channels/${c.id}/credential`);
        modal("Key details", h("div", {},
          h("p", {}, `Key ending ${meta.hint.replace("…", "")} · added ${when(meta.created_at)} · last used ${when(meta.last_used_at)} · rotate by ${meta.rotate_due_at ? new Date(meta.rotate_due_at).toLocaleDateString() : "—"}`),
          h("h3", {}, "Every use of this key"),
          table([{ label: "When", render: (u) => when(u.at) }, { label: "Operation", render: (u) => u.operation },
            { label: "Result", render: (u) => u.result }], usage.data, { empty: "Not used yet." })), { wide: true });
      }) }, "Key details") : null,
      admin && c.connected && c.needs_credential ? h("button", { class: "link danger", onclick: action(async () => {
        if (!await confirmDialog(`Disconnect ${c.name}?`, "The stored key is destroyed immediately and publishing to this channel stops. Listings already on the store are not removed.", "Disconnect", true)) return;
        await api.del(`/channels/${c.id}`); refresh();
      }) }, "Disconnect") : null)));

  clear(el).append(
    h("header", { class: "page" }, h("h1", {}, "Channels"), admin ? h("button", { class: "primary", onclick: add }, "Add channel") : null),
    channels.data.length ? h("div", { class: "cards" }, cards)
      : emptyState("No channels yet", "A channel is a store or marketplace you publish to. Start with the sandbox store to try publishing safely.",
        admin ? h("button", { class: "primary", onclick: add }, "Add a channel") : null));
}

// ------------------------------------------------------------------ jobs

const DONE = ["completed", "partially_completed", "failed", "cancelled", "rolled_back"];

export async function jobsView(el, params) {
  if (params[0]) return jobDetail(el, params[0]);
  const [jobs, catalogs, channels] = await Promise.all([api.get("/jobs"), api.get("/catalogs"), api.get("/channels")]);
  const names = Object.fromEntries(channels.data.map((c) => [c.id, c.name]));
  const usable = channels.data.filter((c) => c.connected && c.role !== "source");

  const create = () => {
    if (!catalogs.data.length || !usable.length) {
      toast(!catalogs.data.length ? "Create a catalog and add products first." : "Connect a channel first.", "error"); return;
    }
    const catalog = h("select", {}, catalogs.data.map((c) => h("option", { value: c.id, selected: c.id === state.catalogId }, `${c.name} (${c.products} products)`)));
    const boxes = usable.map((c) => [c.id, h("input", { type: "checkbox", checked: usable.length === 1 })]);
    const sandbox = h("input", { type: "checkbox" });
    const m = modal("Publish products", h("form", { onsubmit: action(async (event) => {
      event.preventDefault();
      const ids = boxes.filter(([, box]) => box.checked).map(([id]) => id);
      if (!ids.length) { toast("Choose at least one channel.", "error"); return; }
      const job = await api.post("/jobs", { kind: "publish", catalog_id: catalog.value, channel_ids: ids, options: { sandbox: sandbox.checked } },
        { "Idempotency-Key": crypto.randomUUID() });
      m.close(); navigate(`jobs/${job.id}`);
    }) },
    field("Catalog", catalog, "Only products marked active are published."),
    h("fieldset", {}, h("legend", {}, "Channels"), boxes.map(([id, box]) => h("label", { class: "check" }, box, names[id]))),
    h("label", { class: "check" }, sandbox, "Trial run: only the first 10 products"),
    h("p", { class: "muted small" }, "Next you'll see a preview of exactly what would change. Nothing is sent until you approve it."),
    h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Preview changes"))));
  };

  clear(el).append(
    h("header", { class: "page" }, h("h1", {}, "Publishing"), can("jobs:write") ? h("button", { class: "primary", onclick: create }, "Publish products") : null),
    table([
      { label: "Job", render: (j) => h("a", { href: `#/jobs/${j.id}` }, j.kind === "rollback" ? "Rollback" : j.options.sandbox ? "Publish (trial)" : "Publish") },
      { label: "Channels", render: (j) => j.channel_ids.map((id) => names[id] || "Removed channel").join(", ") },
      { label: "Status", render: (j) => stateBadge(j.state) },
      { label: "Progress", render: (j) => progressText(j) },
      { label: "Started", render: (j) => when(j.created_at) },
    ], jobs.data, { empty: "No publishing jobs yet. Publish products to see them here." }));
  return undefined;
}

function progressText(job) {
  const c = job.counts;
  const ok = (c.PUSHED || 0) + (c.ROLLED_BACK || 0) + (c.DELISTED || 0);
  const bad = (c.FAILED || 0) + (c.DEAD_LETTER || 0);
  const waiting = (c.QUEUED || 0) + (c.PUSHING || 0) + (c.RETRY_WAIT || 0) + (c.VALIDATED || 0);
  return [ok ? `${ok} done` : null, waiting ? `${waiting} to go` : null, bad ? `${bad} need attention` : null, c.SKIPPED ? `${c.SKIPPED} unchanged` : null]
    .filter(Boolean).join(" · ") || "—";
}

async function jobDetail(el, jobId) {
  let job = await api.get(`/jobs/${jobId}`);
  const channels = (await api.get("/channels")).data;
  const names = Object.fromEntries(channels.map((c) => [c.id, c.name]));
  const head = h("div", {});
  const itemsBox = h("div", {});
  let filter = "";
  let source = null;
  let alive = true;

  const act = (label, path, body, cls = "", confirm = null) => h("button", { class: cls, onclick: action(async () => {
    if (confirm && !await confirmDialog(confirm[0], confirm[1], label, cls === "danger")) return;
    const result = await api.post(`/jobs/${job.id}/${path}`, body ?? {});
    if (path === "rollbacks") { navigate(`jobs/${result.id}`); return; }
    job = result.job || result;
    drawHead(); await loadItems(); listen();
  }) }, label);

  const drawHead = () => {
    const p = job.preview || {};
    const total = Object.values(job.counts).reduce((a, b) => a + b, 0) || 1;
    const finished = total - ((job.counts.QUEUED || 0) + (job.counts.PUSHING || 0) + (job.counts.RETRY_WAIT || 0));
    const buttons = [];
    if (can("publish:write")) {
      if (job.state === "awaiting_approval") {
        buttons.push(act(job.needs_second_approver ? "Give second approval" : job.kind === "rollback" ? "Approve rollback" : "Approve and publish", "approve", {}, "primary"),
          act("Discard", "control/cancel", {}, "", ["Discard this preview?", "Nothing has been sent to any channel."]));
      }
      if (["running", "rolling_back"].includes(job.state)) buttons.push(act("Pause", "control/pause"), act("Cancel", "control/cancel", {}, "danger", ["Cancel this job?", "Items not yet sent are cancelled. Items already published stay published; you can roll them back afterwards."]));
      if (job.state === "paused") buttons.push(act("Resume", "control/resume", {}, "primary"), act("Cancel", "control/cancel", {}, "danger", ["Cancel this job?", "Items not yet sent are cancelled."]));
      if ((job.counts.FAILED || 0) + (job.counts.DEAD_LETTER || 0) > 0 && job.state !== "awaiting_approval") buttons.push(act("Retry failed items", "items/retry"));
      if (job.kind === "publish" && DONE.includes(job.state) && (job.counts.PUSHED || 0) > 0) {
        buttons.push(act("Roll back…", "rollbacks", {}, "", ["Prepare a rollback?", "You'll see exactly what would be removed or restored before anything happens."]));
      }
    }
    clear(head).append(
      h("header", { class: "page" },
        h("div", {}, h("a", { class: "muted small", href: "#/jobs" }, "← Publishing"),
          h("h1", {}, job.kind === "rollback" ? "Rollback" : "Publish", " ", stateBadge(job.state))),
        h("div", { class: "row" }, buttons)),
      job.needs_second_approver ? h("div", { class: "banner warn" }, "This job removes many listings, so a second person must also approve it.") : null,
      job.state === "awaiting_approval" && job.kind === "publish" ? h("section", { class: "card" }, h("h2", {}, "Preview: nothing has been sent yet"),
        h("div", { class: "cards" }, Object.entries(p.channels || {}).map(([, c]) => h("div", { class: "mini" }, h("strong", {}, c.name),
          h("ul", { class: "plain" },
            h("li", {}, `${c.create} new listing(s)`), h("li", {}, `${c.update} update(s)`), h("li", {}, `${c.unchanged} unchanged (skipped)`),
            c.blocked ? h("li", { class: "bad-text" }, `${c.blocked} blocked by problems (see below)`) : null,
            h("li", { class: "muted" }, `About ${c.estimated_seconds < 60 ? `${c.estimated_seconds} s` : `${Math.ceil(c.estimated_seconds / 60)} min`}, ${c.estimated_api_calls} request(s)`))))),
        p.high_risk ? h("p", { class: "warn-text" }, `${p.high_risk} item(s) change price, stock, brand or category. Review them below; approving will ask you to confirm your identity.`) : null) : null,
      job.state === "awaiting_approval" && job.kind === "rollback" ? h("section", { class: "card" }, h("h2", {}, "What this rollback will do"),
        h("ul", {}, h("li", {}, `Remove ${p.delete} listing(s) the job created`), h("li", {}, `Restore ${p.restore} listing(s) to their previous content`),
          p.cannot_rollback?.length ? h("li", { class: "bad-text" }, `${p.cannot_rollback.length} item(s) cannot be rolled back: ${p.cannot_rollback[0].reason}.`) : null)) : null,
      !["awaiting_approval", "previewing"].includes(job.state) ? h("section", { class: "card" },
        h("progress", { max: String(total), value: String(finished), "aria-label": "Progress" }),
        h("p", {}, progressText(job)),
        h("div", { class: "cards" }, Object.entries(job.counts_by_channel).map(([id, c]) => h("div", { class: "mini" }, h("strong", {}, names[id] || "Channel"),
          h("div", { class: "chips" }, Object.entries(c).map(([s, n]) => h("span", {}, stateBadge(s), ` ${n}`))))))) : null);
  };

  async function loadItems() {
    const params = new URLSearchParams({ limit: "100" });
    if (filter) params.set("state", filter);
    const page = await api.get(`/jobs/${job.id}/items?${params}`);
    clear(itemsBox).append(table([
      { label: "Product", render: (i) => h("div", {}, h("strong", {}, i.title || i.key || "Product"), h("div", { class: "muted small" }, names[i.channel_id] || "")) },
      { label: "Status", render: (i) => h("div", {}, stateBadge(i.waiting && i.state !== "FAILED" ? "paused" : i.state), i.risk === "high" ? h("span", {}, " ", badge("High impact", "bad")) : null) },
      { label: "What changes", render: (i) => {
        if (i.last_error) return h("div", { class: i.state === "RETRY_WAIT" ? "warn-text" : "bad-text" }, i.last_error.message, i.last_error.suggestion ? h("div", { class: "muted small" }, i.last_error.suggestion) : null);
        const errors = i.issues.filter((x) => x.severity === "error");
        if (errors.length) return h("div", { class: "bad-text" }, errors[0].message, h("div", { class: "muted small" }, errors[0].suggestion));
        if (i.diff[0]?.field === "*") return h("span", {}, i.diff[0].after === "new listing" ? "New listing" : show(i.diff[0].after));
        if (!i.diff.length) return h("span", { class: "muted" }, "No change");
        return h("ul", { class: "plain small" }, i.diff.slice(0, 6).map((d) => h("li", {}, h("span", { class: "muted" }, `${d.field}: `),
          h("del", {}, show(d.before).slice(0, 80)), " → ", h("ins", {}, show(d.after).slice(0, 80)))),
        i.diff.length > 6 ? h("li", { class: "muted" }, `and ${i.diff.length - 6} more`) : null);
      } },
      { label: "Notes", render: (i) => { const w = i.issues.filter((x) => x.severity === "warning"); return w.length ? h("span", { class: "muted small" }, w.map((x) => x.message).join(" ")) : ""; } },
    ], page.data, { empty: "No items match." }), page.next_cursor ? h("p", { class: "muted pad" }, "Showing the first 100 items. Use the filter to narrow down.") : null);
  }

  function listen() {
    if (source) { source.close(); source = null; }
    if (DONE.includes(job.state) || job.state === "awaiting_approval") return;
    source = new EventSource(`/v1/events/stream?job_id=${encodeURIComponent(job.id)}`);
    let last = 0;
    source.addEventListener("job.progress", async (event) => {
      const data = JSON.parse(event.data);
      job = { ...job, state: data.state, counts: data.counts, counts_by_channel: data.counts_by_channel };
      drawHead();
      if (Date.now() - last > 3000 || DONE.includes(data.state)) { last = Date.now(); loadItems().catch(() => {}); }
      if (DONE.includes(data.state)) { source.close(); source = null; job = await api.get(`/jobs/${job.id}`); if (alive) drawHead(); }
    });
    source.onerror = () => { /* the browser reconnects on its own; any server can resume the stream */ };
  }

  clear(el).append(head,
    h("div", { class: "toolbar" }, h("h2", {}, "Items"),
      h("select", { "aria-label": "Filter items", onchange: (e) => { filter = e.target.value; loadItems().catch(showError); } },
        [["", "All"], ["FAILED,DEAD_LETTER", "Need attention"], ["VALIDATED,QUEUED,PUSHING,RETRY_WAIT", "To do"], ["PUSHED,ROLLED_BACK", "Done"], ["SKIPPED", "Skipped"]]
          .map(([v, l]) => h("option", { value: v }, l)))),
    itemsBox);
  drawHead(); await loadItems(); listen();
  return () => { alive = false; if (source) source.close(); };
}

// ------------------------------------------------------------------ activity

export async function activityView(el) {
  const status = h("div", {});
  const list = h("div", {});
  let before = null;
  let rows = [];
  const ACTION = (a) => a.replace(/[._]/g, " ");
  async function load() {
    const page = await api.get(`/audit-events?limit=100${before ? `&before=${before}` : ""}`);
    rows = rows.concat(page.data); before = page.next_before;
    clear(list).append(table([
      { label: "When", render: (e) => when(e.at) },
      { label: "Who", render: (e) => (e.actor_type === "system" ? "System" : e.actor_type === "api_key" ? `API key ${e.actor_id}` : "User") },
      { label: "What", render: (e) => ACTION(e.action) },
      { label: "Details", render: (e) => h("span", { class: "muted small clip" }, Object.entries(e.detail).map(([k, v]) => `${k}: ${show(v)}`).join(", ").slice(0, 160)) },
    ], rows, { empty: "No activity yet." }), before ? h("div", { class: "row center pad" }, h("button", { onclick: action(load) }, "Load more")) : null);
  }
  clear(el).append(
    h("header", { class: "page" }, h("h1", {}, "Activity"),
      h("button", { onclick: action(async () => {
        const r = await api.get("/audit-events/verify");
        clear(status).append(h("div", { class: `banner ${r.ok ? "good" : "bad"}`, role: "status" }, r.ok
          ? `Verified: all ${r.events_checked} records are intact and in order.`
          : `Tampering detected at record ${r.broken_at_seq}: ${r.reason}. Contact support.`));
      }) }, "Verify integrity")),
    h("p", { class: "muted" }, "A tamper-evident record of every sign-in, change, publish and key use. Each record is chained to the one before it, so edits or deletions are detectable."),
    status, list);
  await load();
}
