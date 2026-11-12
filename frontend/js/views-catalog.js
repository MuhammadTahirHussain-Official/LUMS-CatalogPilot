import * as api from "./api.js";
import { can, navigate, refresh, setCatalog, state } from "./main.js";
import { action, badge, clear, confirmDialog, emptyState, field, h, modal, money, show, showError, stateBadge, table, toast, when } from "./ui.js";

async function pickCatalog() {
  const catalogs = (await api.get("/catalogs")).data;
  if (!catalogs.some((c) => c.id === state.catalogId)) setCatalog(catalogs[0]?.id || null);
  return catalogs;
}

function createCatalogButton(label = "New catalog") {
  return h("button", { onclick: () => {
    const name = h("input", { type: "text", required: true, maxlength: "200", value: "Main catalog" });
    const currency = h("input", { type: "text", required: true, pattern: "[A-Za-z]{3}", maxlength: "3", value: "USD" });
    const m = modal("New catalog", h("form", { onsubmit: action(async (event) => {
      event.preventDefault();
      const created = await api.post("/catalogs", { name: name.value, default_currency: currency.value.toUpperCase() });
      setCatalog(created.id); m.close(); refresh();
    }) },
    field("Name", name), field("Default currency", currency, "Three-letter code, used when a file has no currency column."),
    h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Create"))));
    name.select();
  } }, label);
}

function catalogSwitcher(catalogs) {
  if (catalogs.length < 2) return null;
  return h("select", { "aria-label": "Catalog", onchange: (e) => { setCatalog(e.target.value); refresh(); } },
    catalogs.map((c) => h("option", { value: c.id, selected: c.id === state.catalogId }, `${c.name} (${c.products})`)));
}

// ------------------------------------------------------------------ catalog

export async function catalogView(el) {
  const catalogs = await pickCatalog();
  if (!state.catalogId) {
    clear(el).append(h("header", { class: "page" }, h("h1", {}, "Catalog")),
      emptyState("No catalog yet", "A catalog holds your products. Create one, then upload a spreadsheet.",
        can("catalog:write") ? createCatalogButton("Create a catalog") : null));
    return;
  }
  const catalog = catalogs.find((c) => c.id === state.catalogId);
  const selected = new Set();
  let query = "";
  let status = "";
  let cursor = null;
  let rows = [];
  const list = h("div", {});
  const bulkBar = h("div", { class: "bulk", hidden: true });

  const drawBulk = () => {
    bulkBar.hidden = selected.size === 0;
    clear(bulkBar).append(h("strong", {}, `${selected.size} selected`),
      ...[["active", "Mark active"], ["draft", "Mark draft"], ["archived", "Archive"]].map(([value, label]) =>
        h("button", { onclick: action(async () => {
          if (value === "archived" && !await confirmDialog("Archive these products?", `${selected.size} product(s) will be hidden. You can restore them from version history.`, "Archive", true)) return;
          await api.post(`/catalogs/${catalog.id}/products:bulk`, { product_ids: [...selected], set_status: value });
          selected.clear(); await load(true);
        }) }, label)));
  };

  const drawList = () => {
    clear(list);
    if (!rows.length) {
      list.append(query || status
        ? h("p", { class: "muted pad" }, "No products match.")
        : emptyState("This catalog is empty", "Upload a CSV or Excel file to add products.",
          can("uploads:write") ? h("a", { class: "button primary", href: "#/uploads" }, "Upload a file") : null));
      return;
    }
    const all = h("input", { type: "checkbox", "aria-label": "Select all", checked: rows.every((p) => selected.has(p.id)),
      onchange: (e) => { rows.forEach((p) => (e.target.checked ? selected.add(p.id) : selected.delete(p.id))); drawList(); drawBulk(); } });
    list.append(h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { class: "tight" }, all), h("th", {}, "Product"), h("th", {}, "Status"),
        h("th", {}, "Variants"), h("th", { class: "right" }, "Price"), h("th", {}, "Updated"))),
      h("tbody", {}, rows.map((p) => {
        const v = p.variants[0] || {};
        const title = Object.values(p.title)[0] || "(untitled)";
        return h("tr", {},
          h("td", { class: "tight" }, h("input", { type: "checkbox", "aria-label": `Select ${title}`, checked: selected.has(p.id),
            onchange: (e) => { if (e.target.checked) selected.add(p.id); else selected.delete(p.id); drawBulk(); } })),
          h("td", {}, h("button", { class: "link strong", onclick: () => productDialog(p.id, () => load(true)) }, title),
            h("div", { class: "muted small" }, [p.brand, v.sku].filter(Boolean).join(" · "))),
          h("td", {}, stateBadge(p.status)), h("td", {}, String(p.variants.length)),
          h("td", { class: "right" }, money(v.price_minor, v.currency)), h("td", {}, when(p.updated_at)));
      })))),
    cursor ? h("div", { class: "row center pad" }, h("button", { onclick: action(() => load(false)) }, "Load more")) : null);
  };

  async function load(reset) {
    if (reset) { rows = []; cursor = null; }
    const params = new URLSearchParams({ limit: "50" });
    if (cursor) params.set("cursor", cursor);
    if (query) params.set("q", query);
    if (status) params.set("status", status);
    const page = await api.get(`/catalogs/${catalog.id}/products?${params}`);
    rows = rows.concat(page.data);
    cursor = page.next_cursor;
    drawList(); drawBulk();
  }

  let timer;
  clear(el).append(
    h("header", { class: "page" }, h("h1", {}, catalog.name),
      h("div", { class: "row" }, catalogSwitcher(catalogs),
        can("catalog:write") ? h("button", { onclick: action(async () => {
          const result = await api.post(`/catalogs/${catalog.id}/enhance`, {});
          toast(result.proposals
            ? `${result.proposals} suggestion(s): ${result.auto_approved} applied automatically, ${result.pending} waiting for your review.`
            : "No improvements to suggest right now.", "good");
          if (result.pending) navigate("review"); else load(true);
        }) }, "Suggest improvements") : null,
        h("button", { onclick: () => versionsDialog(catalog, () => load(true)) }, "History"),
        h("button", { onclick: action(() => api.download("POST", `/catalogs/${catalog.id}/exports?format=csv`, "catalog.csv")) }, "Export CSV"),
        h("button", { onclick: action(() => api.download("POST", `/catalogs/${catalog.id}/exports?format=xlsx`, "catalog.xlsx")) }, "Export Excel"),
        can("catalog:write") ? createCatalogButton() : null)),
    h("div", { class: "toolbar" },
      h("input", { type: "search", placeholder: "Search by title, SKU or handle", "aria-label": "Search products",
        oninput: (e) => { clearTimeout(timer); timer = setTimeout(() => { query = e.target.value.trim(); load(true).catch(showError); }, 250); } }),
      h("select", { "aria-label": "Status", onchange: (e) => { status = e.target.value; load(true).catch(showError); } },
        [["", "Active and draft"], ["active", "Active"], ["draft", "Draft"], ["archived", "Archived"]].map(([v, l]) => h("option", { value: v }, l)))),
    bulkBar, list);
  await load(true);
}

async function productDialog(productId, onSaved) {
  const [p, ready] = await Promise.all([api.get(`/products/${productId}`), api.get(`/products/${productId}/readiness`)]);
  const locale = Object.keys(p.title)[0] || "en";
  const title = h("input", { type: "text", maxlength: "500", required: true, value: p.title[locale] || "" });
  const description = h("textarea", { rows: "6", maxlength: "65000" }, p.description_html[locale] || "");
  const brand = h("input", { type: "text", maxlength: "200", value: p.brand || "" });
  const tags = h("input", { type: "text", value: p.tags.join(", ") });
  const status = h("select", {}, ["draft", "active", "archived"].map((s) => h("option", { value: s, selected: p.status === s }, s)));
  const editable = can("catalog:write");
  const form = h("form", { onsubmit: action(async (event) => {
    event.preventDefault();
    await api.patch(`/products/${p.id}`, {
      title: title.value, description_html: description.value, brand: brand.value, status: status.value,
      tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean),
    }, { "If-Match": String(p.version) });
    toast("Saved.", "good"); m.close(); onSaved();
  }) },
  field("Title", title), field("Description", description, "Basic formatting is kept; scripts and unsafe markup are removed."),
  h("div", { class: "grid2" }, field("Brand", brand), field("Status", status, "Only active products are published.")),
  field("Tags", tags, "Separate with commas."),
  h("h3", {}, "Variants"),
  table([
    { label: "SKU", render: (v) => v.sku || "—" },
    { label: "Options", render: (v) => Object.entries(v.option_values || {}).map(([k, val]) => `${k}: ${val}`).join(", ") || "—" },
    { label: "Price", class: "right", render: (v) => money(v.price_minor, v.currency) },
    ...(p.variants.some((v) => "cost_minor" in v) ? [{ label: "Cost", class: "right", render: (v) => money(v.cost_minor, v.currency) }] : []),
    { label: "Stock", class: "right", render: (v) => (v.inventory ?? "—") },
    { label: "Barcode", render: (v) => v.gtin || "—" },
  ], p.variants),
  p.attributes.length ? h("div", {}, h("h3", {}, "Attributes"),
    h("div", { class: "chips" }, p.attributes.map((a) => badge(`${a.key}: ${a.value}`)))) : null,
  h("h3", {}, "Ready to publish?"),
  ready.data.length ? h("ul", { class: "plain" }, ready.data.map((r) => h("li", {},
    stateBadge(r.ready ? "ok" : "failed"), " ", h("strong", {}, r.channel),
    r.issues.length ? h("ul", {}, r.issues.map((i) => h("li", { class: i.severity === "error" ? "bad-text" : "muted" },
      `${i.message} ${i.suggestion || ""}`))) : " No issues."))) : h("p", { class: "muted" }, "Connect a channel to see what it requires."),
  editable ? h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Save")) : null);
  const m = modal(p.title[locale] || "Product", form, { wide: true });
}

async function versionsDialog(catalog, onRestored) {
  const versions = await api.get(`/catalogs/${catalog.id}/versions`);
  const SOURCE = { upload: "File import", bulk_edit: "Edit", ai_apply: "AI suggestions applied", restore: "Restored", import: "Import", sync: "Sync" };
  const body = h("div", {}, h("p", { class: "muted" }, "Every import and edit is a version. Restoring creates a new version, so nothing is ever lost."),
    table([
      { label: "Version", render: (v) => `#${v.version}${v.version === versions.head_version ? " (current)" : ""}` },
      { label: "What happened", render: (v) => SOURCE[v.source] || v.source },
      { label: "Changes", render: (v) => Object.entries(v.summary).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(", ") },
      { label: "When", render: (v) => when(v.created_at) },
      { label: "", class: "right", render: (v) => h("div", { class: "row end" },
        h("button", { class: "link", onclick: action(async () => {
          const diff = await api.get(`/catalogs/${catalog.id}/versions/${v.version}/diff?limit=50`);
          modal(`Version #${v.version}: ${diff.total} product(s) changed`, h("div", { class: "stack" }, diff.data.map((d) =>
            h("div", { class: "diff-item" }, h("strong", {}, d.external_key), " ", badge(d.op),
              d.op === "created" ? null : h("ul", { class: "plain" }, d.fields.map((f) => h("li", {}, h("span", { class: "muted" }, `${f.field}: `),
                h("del", {}, show(f.before).slice(0, 200)), " → ", h("ins", {}, show(f.after).slice(0, 200)))))))), { wide: true });
        }) }, "View changes"),
        can("catalog:write") && v.version !== versions.head_version ? h("button", { class: "link", onclick: action(async () => {
          if (!await confirmDialog(`Restore version #${v.version}?`, "The catalog goes back to how it was at that version. Published listings are not changed until you publish again.", "Restore")) return;
          const result = await api.post(`/catalogs/${catalog.id}/versions/${v.version}/restore`);
          toast(result.changed ? `Restored as version #${result.new_version}.` : "Already identical to that version.", "good");
          m.close(); onRestored();
        }) }, "Restore") : null) },
    ], versions.data, { empty: "No changes yet." }));
  const m = modal("Version history", body, { wide: true });
}

// ------------------------------------------------------------------ uploads

export async function uploadsView(el, params) {
  const catalogs = await pickCatalog();
  if (params[0] && params[1]) return mappingView(el, params[0], params[1]);
  if (!state.catalogId) {
    clear(el).append(h("header", { class: "page" }, h("h1", {}, "Uploads")),
      emptyState("Create a catalog first", "Uploaded products go into a catalog.", createCatalogButton("Create a catalog")));
    return undefined;
  }
  const progress = h("div", { class: "stack" });
  const history = h("div", {});
  let alive = true;
  let poll;

  const drawHistory = async () => {
    const uploads = (await api.get("/uploads")).data;
    if (!alive) return;
    const files = uploads.flatMap((u) => u.files.map((f) => ({ ...f, upload: u })));
    clear(history).append(h("h2", {}, "Recent files"), table([
      { label: "File", render: (f) => f.name },
      { label: "Status", render: (f) => h("div", {}, stateBadge(f.state),
        f.rejected ? h("div", { class: "bad-text small" }, f.rejected.map((r) => r.detail).join("; ")) : null,
        f.media.length ? h("div", { class: "muted small" }, `${f.media.filter((x) => x.status === "stored").length} image(s) stored, ${f.media.filter((x) => x.status !== "stored").length} skipped`) : null) },
      { label: "Result", render: (f) => (f.import_result
        ? `${f.import_result.products.created} new, ${f.import_result.products.updated} updated, ${f.import_result.row_errors} row error(s)` : "—") },
      { label: "Uploaded", render: (f) => when(f.upload.created_at) },
      { label: "", class: "right", render: (f) => {
        const data = ["csv", "tsv", "xlsx", "json", "ndjson", "xml"].includes(f.detected_type);
        if (f.state === "clean" && data) return h("a", { class: "button", href: `#/uploads/${f.upload.id}/${f.id}` }, f.import_result ? "Import again" : "Map columns & import");
        if (f.state === "received" || f.state === "scanning") return h("span", { class: "muted" }, "Checking…");
        return "";
      } },
    ], files, { empty: "No files uploaded yet." }));
    clearTimeout(poll);
    if (files.some((f) => ["received", "scanning", "receiving"].includes(f.state))) poll = setTimeout(() => drawHistory().catch(() => {}), 2000);
  };

  const input = h("input", { type: "file", multiple: true, class: "sr", id: "file-input",
    accept: ".csv,.tsv,.txt,.xlsx,.json,.ndjson,.jsonl,.xml,.zip,.jpg,.jpeg,.png,.webp,.gif,.pdf",
    onchange: async (event) => {
      const chosen = [...event.target.files];
      event.target.value = "";
      for (const file of chosen) {
        const bar = h("progress", { max: "1", value: "0" });
        const label = h("span", { class: "muted" }, "Starting…");
        const row = h("div", { class: "upload-row" }, h("strong", {}, file.name), bar, label);
        progress.append(row);
        try {
          const { uploadId, fileId } = await api.uploadFile(state.catalogId, file, (fraction, text) => { bar.value = fraction; label.textContent = text; });
          bar.value = 1; label.textContent = "Uploaded. Checking for threats…";
          await drawHistory();
          const info = (await api.get(`/uploads/${uploadId}`)).files[0];
          if (info.state === "clean" && ["csv", "tsv", "xlsx", "json", "ndjson", "xml"].includes(info.detected_type) && chosen.length === 1) {
            navigate(`uploads/${uploadId}/${fileId}`);
          }
          row.remove();
        } catch (error) {
          label.textContent = error.message; label.className = "bad-text";
        }
      }
    } });
  clear(el).append(
    h("header", { class: "page" }, h("h1", {}, "Uploads"), h("div", { class: "row" }, catalogSwitcher(catalogs))),
    h("section", { class: "card dropzone" },
      h("h2", {}, "Upload product data or images"),
      h("p", { class: "muted" }, "CSV, TSV, Excel (.xlsx), JSON, XML, or a ZIP of images named by SKU. Every file is checked for threats before anything is read."),
      input, h("label", { class: "button primary", for: "file-input" }, "Choose files"),
      progress),
    history);
  await drawHistory();
  return () => { alive = false; clearTimeout(poll); };
}

async function mappingView(el, uploadId, fileId) {
  const base = `/uploads/${uploadId}/files/${fileId}`;
  const preview = await api.get(`${base}/preview`);
  const suggestion = preview.mapping || preview.suggested_mapping;
  const mapping = { ...suggestion, columns: { ...suggestion.columns } };
  const LABELS = { external_id: "Product ID", group_id: "Variant group ID", handle: "Handle / URL slug", title: "Title", description: "Description",
    brand: "Brand", product_type: "Product type", category: "Category", tags: "Tags", status: "Status", seo_title: "SEO title",
    seo_description: "SEO description", image_urls: "Image URL(s)", sku: "SKU", gtin: "Barcode (GTIN)", mpn: "MPN", price: "Price",
    compare_at_price: "Compare-at price", cost: "Cost", currency: "Currency", inventory: "Stock quantity", weight: "Weight",
    option1_name: "Option 1 name", option1_value: "Option 1 value", option2_name: "Option 2 name", option2_value: "Option 2 value",
    option3_name: "Option 3 name", option3_value: "Option 3 value" };
  const keySelect = h("select", {});
  const drawKeys = () => {
    const mapped = new Set(Object.values(mapping.columns));
    const options = preview.key_fields.filter((k) => mapped.has(k));
    if (!options.includes(mapping.key)) mapping.key = options[0] || null;
    clear(keySelect).append(options.length ? options.map((k) => h("option", { value: k, selected: k === mapping.key }, LABELS[k])) : h("option", { value: "" }, "Map SKU, handle, barcode or product ID first"));
  };
  keySelect.addEventListener("change", () => { mapping.key = keySelect.value; });
  const rows = preview.columns.map((column) => {
    const current = mapping.columns[column];
    const select = h("select", { "aria-label": `Field for ${column}`, onchange: (e) => {
      const value = e.target.value;
      mapping.columns[column] = value === "" ? null : value === "attr" ? `attribute:${column.slice(0, 60)}` : value;
      drawKeys();
    } },
    h("option", { value: "", selected: !current }, "Don't import"),
    h("option", { value: "attr", selected: Boolean(current?.startsWith("attribute:")) }, "Keep as attribute"),
    preview.target_fields.map((t) => h("option", { value: t, selected: current === t }, LABELS[t] || t)));
    const samples = preview.rows.map((r) => r[column]).filter(Boolean).slice(0, 3).join(" · ");
    return h("tr", {}, h("td", {}, h("strong", {}, column)), h("td", { class: "muted clip" }, samples || "(empty)"), h("td", {}, select));
  });
  drawKeys();
  const template = h("input", { type: "text", maxlength: "120", placeholder: "Optional: save as template, e.g. Supplier A" });
  const result = h("div", {});
  clear(el).append(
    h("header", { class: "page" }, h("h1", {}, "Map columns"), h("a", { class: "button", href: "#/uploads" }, "Back to uploads")),
    preview.warnings.length ? h("div", { class: "banner warn" }, preview.warnings.join(" ")) : null,
    h("p", { class: "muted" }, "We matched your columns to product fields. Check the suggestions, then import. Nothing is published by importing."),
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Your column"), h("th", {}, "Sample values"), h("th", {}, "Import as"))), h("tbody", {}, rows))),
    h("div", { class: "grid2" },
      field("Match existing products by", keySelect, "Re-uploading a file updates products with the same key instead of duplicating them."),
      field("Template", template)),
    h("div", { class: "row end" }, h("button", { class: "primary", onclick: action(async () => {
      await api.put(`${base}/mapping`, { mapping, save_as: template.value.trim() || null });
      const r = await api.post(`${base}/import`);
      clear(result).append(h("section", { class: "card" }, h("h2", {}, "Import finished"),
        h("p", {}, `${r.products.created} created, ${r.products.updated} updated, ${r.products.unchanged} unchanged from ${r.total_rows} row(s).`),
        r.row_errors ? h("p", { class: "bad-text" }, `${r.row_errors} row(s) were skipped. `,
          h("button", { class: "link", onclick: action(() => api.download("GET", `${base}/errors.csv`, "import-errors.csv")) }, "Download the error file")) : null,
        r.issues.length ? h("ul", { class: "plain small" }, r.issues.slice(0, 20).map((i) => h("li", { class: i.level === "error" ? "bad-text" : "muted" }, `Row ${i.row}: ${i.message}`))) : null,
        r.unmatched_media.length ? h("p", { class: "muted" }, `${r.unmatched_media.length} image(s) did not match any SKU.`) : null,
        h("p", { class: "muted" }, "New products start as drafts. Mark them active in the catalog when they are ready to publish."),
        h("a", { class: "button primary", href: "#/catalog" }, "Go to catalog")));
      result.scrollIntoView({ behavior: "smooth" });
    }) }, "Import products")),
    result);
  return undefined;
}

// ------------------------------------------------------------------ review

export async function reviewView(el) {
  let task = "";
  const selected = new Set();
  const list = h("div", {});
  const bar = h("div", { class: "bulk", hidden: true });
  const TASK = { title: "Title", description: "Description", attributes: "Attributes", category: "Category" };
  const canDecide = can("reviews:write");

  const decide = async (ids, decision, value) => {
    const r = await api.post("/proposals:decide", { proposal_ids: ids, decision, value });
    const parts = [];
    if (r.applied) parts.push(`${r.applied} applied`);
    if (r.rejected) parts.push(`${r.rejected} rejected`);
    if (r.superseded) parts.push(`${r.superseded} skipped because the product changed since`);
    toast(parts.join(", ") || "Nothing to do.", "good");
    ids.forEach((id) => selected.delete(id));
    await load();
  };

  const drawBar = () => {
    bar.hidden = selected.size === 0 || !canDecide;
    clear(bar).append(h("strong", {}, `${selected.size} selected`),
      h("button", { class: "primary", onclick: action(() => decide([...selected], "approve")) }, "Approve"),
      h("button", { onclick: action(() => decide([...selected], "reject")) }, "Reject"));
  };

  async function load() {
    const params = new URLSearchParams({ status: "pending", limit: "100" });
    if (task) params.set("task", task);
    const page = await api.get(`/proposals?${params}`);
    clear(list);
    if (!page.data.length) {
      list.append(emptyState("Nothing to review", "Suggestions appear here after you choose “Suggest improvements” in the catalog.",
        h("a", { class: "button", href: "#/catalog" }, "Go to catalog")));
      drawBar(); return;
    }
    list.append(h("div", { class: "row" }, h("label", { class: "check" }, h("input", { type: "checkbox",
      checked: page.data.every((p) => selected.has(p.id)),
      onchange: (e) => { page.data.forEach((p) => (e.target.checked ? selected.add(p.id) : selected.delete(p.id))); load().catch(showError); } }), "Select all on this page")));
    for (const p of page.data) {
      const before = p.task === "category" ? (p.original_label || "(none)") : show(p.original);
      const after = p.task === "category" ? (p.proposed_label || show(p.proposed)) : show(p.proposed);
      list.append(h("article", { class: "proposal" },
        h("div", { class: "row" },
          canDecide ? h("input", { type: "checkbox", "aria-label": `Select suggestion for ${p.product_key}`, checked: selected.has(p.id),
            onchange: (e) => { if (e.target.checked) selected.add(p.id); else selected.delete(p.id); drawBar(); } }) : null,
          h("strong", {}, p.product_key), badge(TASK[p.task] || p.task),
          badge(`${Math.round(p.confidence * 100)}% confidence`, p.confidence >= 0.9 ? "good" : p.confidence >= 0.6 ? "info" : "warn"),
          p.risk === "high" ? badge("High impact", "bad") : null),
        h("div", { class: "beforeafter" },
          h("div", {}, h("div", { class: "muted small" }, "Now"), h("div", { class: "old" }, before)),
          h("div", {}, h("div", { class: "muted small" }, "Suggested"), h("div", { class: "new" }, after))),
        p.rationale ? h("p", { class: "muted small" }, p.rationale) : null,
        canDecide ? h("div", { class: "row end" },
          ["title", "description"].includes(p.task) ? h("button", { onclick: () => {
            const input = h("textarea", { rows: p.task === "title" ? "2" : "8" }, String(p.proposed));
            const m = modal("Edit before approving", h("form", { onsubmit: action(async (event) => {
              event.preventDefault(); m.close(); await decide([p.id], "edit", input.value);
            }) }, field("Your version", input), h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Save and approve"))));
          } }, "Edit") : null,
          h("button", { onclick: action(() => decide([p.id], "reject")) }, "Reject"),
          h("button", { class: "primary", onclick: action(() => decide([p.id], "approve")) }, "Approve")) : null));
    }
    drawBar();
  }

  clear(el).append(
    h("header", { class: "page" }, h("h1", {}, "Review suggestions"),
      h("select", { "aria-label": "Type", onchange: (e) => { task = e.target.value; selected.clear(); load().catch(showError); } },
        h("option", { value: "" }, "All types"), Object.entries(TASK).map(([v, l]) => h("option", { value: v }, l)))),
    h("p", { class: "muted" }, "Suggestions never change your products until approved. Approved changes are versioned and can be undone from catalog history."),
    bar, list);
  await load();
}
