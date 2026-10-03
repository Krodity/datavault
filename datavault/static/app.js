/* DataVault frontend — dependency-free SPA (hash router + fetch). */
"use strict";

// ================================================================ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (cents, cur = "USD") => (cents == null ? "—" :
  new Intl.NumberFormat(undefined, { style: "currency", currency: cur || "USD" }).format(cents / 100));
const fmtBytes = (b) => b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;
const fmtDate = (d) => d ? new Date(d.length === 10 ? d + "T00:00:00" : d).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";
const ago = (iso) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};
const today = () => new Date().toLocaleDateString("en-CA");
const debounce = (fn, ms = 250) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== "" && v != null)).toString();
const PALETTE = ["#4f46e5", "#0891b2", "#16a34a", "#ea580c", "#db2777", "#9333ea", "#ca8a04", "#2563eb"];
const colorFor = (s) => PALETTE[[...String(s)].reduce((a, c) => a + c.charCodeAt(0), 0) % PALETTE.length];

class ApiError extends Error {
  constructor(status, data) { super(data?.message || `HTTP ${status}`); this.status = status; this.data = data; }
  get fields() { return this.data?.fields || {}; }
}

async function api(path, { method = "GET", body, form, raw } = {}) {
  const opts = { method, headers: { "X-DataVault": "1" } };
  if (form) opts.body = form;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const res = await fetch(path, opts);
  if (raw && res.ok) return res;
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new ApiError(res.status, typeof data === "object" ? data : { message: data });
  return data;
}

function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), type === "error" ? 6000 : 3000);
}
const fail = (e) => { console.error(e); toast(e.message || String(e), "error"); };

function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// ------------------------------------------------------------------ modal
function modal({ title, body = "", foot = "", wide = false, onMount }) {
  const root = document.createElement("div");
  root.className = "modal-backdrop";
  root.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">
      <header><h2>${esc(title)}</h2><button class="icon-btn" data-close aria-label="Close">✕</button></header>
      <div class="body">${body}</div>${foot ? `<footer>${foot}</footer>` : ""}</div>`;
  const close = () => { root.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  root.addEventListener("mousedown", (e) => { if (e.target === root) close(); });
  root.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) close(); });
  document.addEventListener("keydown", onKey);
  $("#modal-root").append(root);
  const m = { el: root, close, $: (s) => $(s, root), $$: (s) => $$(s, root) };
  onMount?.(m);
  setTimeout(() => root.querySelector("input:not([type=hidden]), textarea, select")?.focus(), 30);
  return m;
}

function confirmBox(message, { ok = "Delete", danger = true } = {}) {
  return new Promise((resolve) => {
    const m = modal({
      title: "Are you sure?", body: `<p>${esc(message)}</p>`,
      foot: `<button class="ghost" data-close>Cancel</button><button class="${danger ? "danger" : ""}" data-ok>${esc(ok)}</button>`,
    });
    m.$("[data-ok]").addEventListener("click", () => { m.close(); resolve(true); });
    m.el.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) resolve(false); });
  });
}

// --------------------------------------------------------- generic forms
// Field spec: {name, label, type, required, options:[[value,label]], full, placeholder, hint}
function formHtml(fields, values = {}) {
  return `<div class="form-grid">${fields.map((f) => {
    const v = values[f.name] ?? f.default ?? "";
    const req = f.required ? "required" : "";
    let input;
    if (f.type === "textarea") input = `<textarea name="${f.name}" rows="${f.rows || 3}" ${req} placeholder="${esc(f.placeholder || "")}">${esc(v)}</textarea>`;
    else if (f.type === "select") input = `<select name="${f.name}" ${req}>${(f.options || []).map(([ov, ol]) =>
      `<option value="${esc(ov)}" ${String(ov) === String(v ?? "") ? "selected" : ""}>${esc(ol)}</option>`).join("")}</select>`;
    else if (f.type === "checkbox") return `<label class="check ${f.full ? "full" : ""}"><input type="checkbox" name="${f.name}" ${v ? "checked" : ""}> ${esc(f.label)}</label>`;
    else input = `<input name="${f.name}" type="${f.type || "text"}" value="${esc(v)}" ${req} ${f.step ? `step="${f.step}"` : ""}
                   placeholder="${esc(f.placeholder || "")}" ${f.list ? `list="${f.list}"` : ""} autocomplete="off">`;
    return `<label class="field ${f.full ? "full" : ""}"><span class="${f.required ? "req" : ""}">${esc(f.label)}</span>${input}
            ${f.hint ? `<span class="muted small" style="font-weight:400">${esc(f.hint)}</span>` : ""}
            <span class="field-error" data-err="${f.name}"></span></label>`;
  }).join("")}</div>`;
}

function readForm(root, fields) {
  const out = {};
  for (const f of fields) {
    const el = root.querySelector(`[name="${f.name}"]`);
    if (!el) continue;
    out[f.name] = f.type === "checkbox" ? el.checked : el.value;
  }
  return out;
}

function showErrors(root, err) {
  $$(".field-error", root).forEach((e) => (e.textContent = ""));
  const fields = err instanceof ApiError ? err.fields : {};
  let shown = false;
  for (const [k, msg] of Object.entries(fields)) {
    const slot = root.querySelector(`[data-err="${k}"]`);
    if (slot) { slot.textContent = msg; shown = true; }
  }
  if (!shown || fields._) toast(fields._ || err.message, "error");
}

// ================================================================ router
const routes = [];
const route = (pattern, view) => routes.push([new RegExp(`^${pattern}$`), view]);
let currentCleanup = null;

async function navigate() {
  const hash = location.hash.slice(1) || "/";
  const [path, query = ""] = hash.split("?");
  const params = Object.fromEntries(new URLSearchParams(query));
  currentCleanup?.();
  currentCleanup = null;
  $("#sidebar").classList.remove("open");
  for (const [re, view] of routes) {
    const m = path.match(re);
    if (!m) continue;
    const navKey = path.split("/")[1] || "dashboard";
    $$(".nav a").forEach((a) => a.classList.toggle("active",
      a.dataset.nav === navKey || a.getAttribute("href") === `#${path}` || (navKey === "s" && a.getAttribute("href") === `#/s/${m[1]}`)));
    const el = $("#view");
    try {
      currentCleanup = (await view(el, ...m.slice(1).map((x) => (x == null ? x : decodeURIComponent(x))), params)) || null;
    } catch (e) {
      el.innerHTML = `<div class="card empty"><b>Something went wrong</b>${esc(e.message)}</div>`;
      fail(e);
    }
    return;
  }
  $("#view").innerHTML = `<div class="card empty"><b>Not found</b>No page at ${esc(path)}</div>`;
}

// ============================================================== dashboard
function barChart(data, { value, label, highlightLast = true, fmt = (v) => v, height = 220 }) {
  const W = 640, H = height, pad = { l: 46, r: 8, t: 12, b: 26 };
  const max = Math.max(1, ...data.map(value));
  const nice = Math.pow(10, Math.floor(Math.log10(max)));
  const top = Math.ceil(max / nice) * nice;
  const bw = (W - pad.l - pad.r) / data.length;
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - v / top);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">`;
  for (let i = 0; i <= 4; i++) {
    const v = (top / 4) * i;
    svg += `<line class="grid-line" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/>
            <text x="${pad.l - 6}" y="${y(v) + 3}" text-anchor="end">${esc(fmt(v))}</text>`;
  }
  data.forEach((d, i) => {
    const v = value(d), x = pad.l + i * bw + bw * 0.18, w = bw * 0.64;
    svg += `<rect class="bar ${highlightLast && i === data.length - 1 ? "cur" : ""}" x="${x}" y="${y(v)}" width="${w}" height="${Math.max(0, H - pad.b - y(v))}" rx="3">
              <title>${esc(label(d))}: ${esc(fmt(v))}</title></rect>
            <text x="${x + w / 2}" y="${H - 8}" text-anchor="middle">${esc(label(d))}</text>`;
  });
  return svg + "</svg>";
}

const monthLabel = (m) => new Date(m + "-01T00:00:00").toLocaleDateString(undefined, { month: "short" });
const shortMoney = (c) => { const d = c / 100; return d >= 1000 ? `$${(d / 1000).toFixed(1)}k` : `$${Math.round(d)}`; };

function budgetBars(cats) {
  if (!cats.length) return `<div class="empty">No spending this month</div>`;
  return cats.map((c) => {
    const pct = c.budget_cents ? Math.min(100, (c.total_cents / c.budget_cents) * 100) : 100;
    const over = c.budget_cents && c.total_cents > c.budget_cents;
    return `<div class="budget"><div class="row"><span><span class="dot" style="background:${esc(c.color)}"></span> ${esc(c.category)}</span>
      <span class="num">${money(c.total_cents)}${c.budget_cents ? ` <span class="muted">/ ${money(c.budget_cents)}</span>` : ""}</span></div>
      <div class="meter"><div style="width:${pct}%;background:${over ? "var(--danger)" : esc(c.color)};opacity:${c.budget_cents ? 1 : .35}"></div></div></div>`;
  }).join("");
}

route("/", async (el) => {
  const [s, sum] = await Promise.all([api("/api/stats"), api("/api/expenses/summary?months=12")]);
  const c = s.counts, t = sum.totals;
  const delta = t.prev_month_cents ? ((t.month_cents - t.prev_month_cents) / t.prev_month_cents) * 100 : null;
  const verb = { INSERT: "Added", UPDATE: "Updated", DELETE: "Deleted" };
  const linkFor = (a) => ({ contacts: `#/contacts/${a.row_id}`, expenses: `#/expenses?edit=${a.row_id}`, pictures: `#/pictures?open=${a.row_id}`,
    codes: "#/codes", section_records: null, notes: `#/notes/${a.row_id}`, folders: `#/folders/${a.row_id}`, files: null }[a.table_name]);
  el.innerHTML = `
    <div class="page-head"><div><h1>Dashboard</h1><p>Everything in your vault at a glance.</p></div>
      <div class="actions"><a class="btn ghost" href="#/sql">Open SQL console</a><a class="btn" href="#/expenses?new=1">＋ Expense</a></div></div>
    <div class="grid cols-5" style="margin-bottom:16px">
      <a class="card stat" href="#/expenses"><span class="label">Spent in ${esc(monthLabel(sum.month))}</span>
        <span class="value">${money(t.month_cents)}</span>
        <span class="delta ${delta > 0 ? "up" : "down"}">${delta == null ? "&nbsp;" : `${delta > 0 ? "▲" : "▼"} ${Math.abs(delta).toFixed(0)}% vs last month`}</span></a>
      <a class="card stat" href="#/contacts"><span class="label">Contacts</span><span class="value">${c.contacts}</span><span class="muted small">${c.tags} tags</span></a>
      <a class="card stat" href="#/pictures"><span class="label">Pictures</span><span class="value">${c.pictures}</span><span class="muted small">${fmtBytes(c.media_bytes)}</span></a>
      <a class="card stat" href="#/codes"><span class="label">Codes · Records</span><span class="value">${c.codes} · ${c.records}</span><span class="muted small">${c.sections} custom sections</span></a>
      <a class="card stat" href="#/folders"><span class="label">Folders</span><span class="value">${c.folders}</span><span class="muted small">${c.notes} notes · ${c.files} files</span></a>
    </div>
    <div class="grid span" style="margin-bottom:16px">
      <div class="card"><h3>Monthly spending</h3>${barChart(sum.trend, { value: (d) => d.total_cents, label: (d) => monthLabel(d.month), fmt: shortMoney })}</div>
      <div class="card"><h3>This month by category</h3>${budgetBars(sum.by_category)}</div>
    </div>
    <div class="grid cols-3">
      <div class="card"><h3>Recent activity</h3>${s.activity.length ? s.activity.map((a) => {
        const href = a.action !== "DELETE" ? linkFor(a) : null;
        const lbl = esc(a.label || `${a.table_name} #${a.row_id}`);
        return `<div class="list-row"><span class="pill">${verb[a.action]}</span>
          <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${href ? `<a href="${href}">${lbl}</a>` : lbl}
          <span class="muted small"> · ${esc(a.table_name.replace("_", " "))}</span></span><span class="muted small nowrap">${ago(a.at)}</span></div>`;
      }).join("") : `<div class="empty">No activity yet</div>`}</div>
      <div class="card"><h3>Top merchants · 12 mo</h3>${sum.top_merchants.map((m) =>
        `<div class="list-row"><span style="flex:1">${esc(m.merchant)}</span><span class="muted small">${m.n}×</span><span class="num">${money(m.total_cents)}</span></div>`).join("") || `<div class="empty">—</div>`}</div>
      <div class="card"><h3>Upcoming birthdays</h3>${s.upcoming_birthdays.map((b) =>
        `<div class="list-row"><a href="#/contacts/${b.id}" style="flex:1">${esc(b.name)}</a>
         <span class="muted small">${b.days_until === 0 ? "🎉 today" : `in ${b.days_until} day${b.days_until === 1 ? "" : "s"}`}</span></div>`).join("") || `<div class="empty">None in the next 30 days</div>`}
        <h3 style="margin-top:18px">Database</h3>
        <div class="list-row"><span style="flex:1">File size</span><span class="num">${fmtBytes(s.db_bytes)}</span></div>
        <div class="list-row"><span style="flex:1">Schema version</span><span class="num">v${s.schema_version}</span></div>
      </div>
    </div>`;
});

// ================================================================ history
const FIELD_NAMES = { first_name: "First name", last_name: "Last name", amount_cents: "Amount", spent_on: "Date",
  category_id: "Category", contact_id: "Contact", receipt_picture_id: "Receipt", photo_id: "Photo", job_title: "Job title",
  postal_code: "Postal code", payment_method: "Payment method", scan_count: "Times scanned" };
const fmtVal = (field, v) => {
  if (v == null || v === "") return `<span class="muted">empty</span>`;
  if (field === "amount_cents" || field === "monthly_budget_cents") return esc(money(v));
  if (Array.isArray(v)) return esc(v.join(", ") || "none");
  if (typeof v === "boolean") return v ? "yes" : "no";
  return esc(String(v).length > 120 ? String(v).slice(0, 120) + "…" : v);
};

async function historyModal(table, id, title) {
  const h = await api(`/api/history/${table}/${id}`);
  const verb = { INSERT: "Created", UPDATE: "Changed", DELETE: "Deleted" };
  modal({ title: `History — ${title}`, wide: true, body: h.length ? h.map((e) => `<div class="hist">
      <div class="hist-head"><span class="pill">${verb[e.action]}</span><span class="muted small">${esc(new Date(e.at).toLocaleString())}</span></div>
      ${e.action === "UPDATE" ? (e.changes.length ? `<table class="diff"><tbody>${e.changes.map((c) => `<tr><td class="muted">${esc(FIELD_NAMES[c.field] || (c.field[0].toUpperCase() + c.field.slice(1)).replace(/_/g, " "))}</td>
        <td class="old">${fmtVal(c.field, c.old)}</td><td>→</td><td class="new">${fmtVal(c.field, c.new)}</td></tr>`).join("")}</tbody></table>`
        : `<div class="muted small">no visible field changes</div>`) : ""}
      ${!e.snapshot ? `<div class="muted small">(recorded before change snapshots were added)</div>` : ""}</div>`).join("")
    : `<div class="empty"><b>No history</b></div>` });
}

// =============================================================== contacts
const CONTACT_FIELDS = [
  { name: "first_name", label: "First name", required: true }, { name: "last_name", label: "Last name" },
  { name: "company", label: "Company" }, { name: "job_title", label: "Job title" },
  { name: "email", label: "Email", type: "email" }, { name: "phone", label: "Phone", type: "tel" },
  { name: "address", label: "Street address", full: true },
  { name: "city", label: "City" }, { name: "region", label: "State / region" },
  { name: "postal_code", label: "Postal code" }, { name: "country", label: "Country" },
  { name: "birthday", label: "Birthday", type: "date" }, { name: "website", label: "Website", placeholder: "example.com" },
  { name: "tags", label: "Tags", full: true, placeholder: "friend, work, …", hint: "Comma-separated; new tags are created automatically", list: "tag-list" },
  { name: "notes", label: "Notes", type: "textarea", full: true },
  { name: "favorite", label: "★ Favorite", type: "checkbox", full: true },
];

function avatar(c, cls = "") {
  if (c.photo_id) return `<img class="avatar ${cls}" src="/media/${c.photo_id}/thumb" alt="">`;
  const initials = ((c.first_name?.[0] || "") + (c.last_name?.[0] || "")).toUpperCase() || "?";
  return `<span class="avatar ${cls}" style="background:${colorFor(c.first_name + c.last_name)}">${esc(initials)}</span>`;
}

async function contactForm(existing, onSaved) {
  const tags = await api("/api/tags");
  const values = existing ? { ...existing, tags: existing.tags.map((t) => t.name).join(", ") } : {};
  const m = modal({
    title: existing ? `Edit ${existing.first_name}` : "New contact", wide: true,
    body: `<datalist id="tag-list">${tags.map((t) => `<option value="${esc(t.name)}">`).join("")}</datalist>
      ${formHtml(CONTACT_FIELDS, values)}
      <div style="margin-top:14px;display:flex;gap:12px;align-items:center" class="full">
        <div id="photo-prev">${existing?.photo_id ? `<img class="avatar lg" src="/media/${existing.photo_id}/thumb">` : ""}</div>
        <label class="btn ghost small">📷 ${existing?.photo_id ? "Change" : "Add"} photo<input type="file" accept="image/*,.heic,.heif" hidden id="photo-in"></label>
        ${existing?.photo_id ? `<button class="ghost small danger" id="photo-rm">Remove photo</button>` : ""}
      </div>`,
    foot: `<button class="ghost" data-close>Cancel</button><button data-save>${existing ? "Save changes" : "Create contact"}</button>`,
  });
  let photoId = existing?.photo_id ?? null;
  m.$("#photo-in").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData(); fd.append("file", f); fd.append("album", "Contacts");
    try {
      const r = await api("/api/pictures", { method: "POST", form: fd });
      photoId = r.items[0].id;
      m.$("#photo-prev").innerHTML = `<img class="avatar lg" src="/media/${photoId}/thumb">`;
    } catch (err) { fail(err); }
  });
  m.$("#photo-rm")?.addEventListener("click", () => { photoId = null; m.$("#photo-prev").innerHTML = ""; });
  m.$("[data-save]").addEventListener("click", async () => {
    const data = { ...readForm(m.el, CONTACT_FIELDS), photo_id: photoId };
    try {
      const saved = existing ? await api(`/api/contacts/${existing.id}`, { method: "PATCH", body: data })
        : await api("/api/contacts", { method: "POST", body: data });
      m.close();
      toast(existing ? "Contact updated" : "Contact created");
      onSaved(saved);
    } catch (e) { showErrors(m.el, e); }
  });
}

route("/contacts(?:/(\\d+))?", async (el, id, params) => {
  const state = { q: params.q || "", tag: params.tag || "", sort: "name", selected: id ? +id : null, limit: 200 };
  el.innerHTML = `
    <div class="page-head"><div><h1>Contacts</h1><p>People, companies and how to reach them.</p></div>
      <div class="actions">
        <button class="ghost" id="dupes">Find duplicates</button>
        <a class="btn ghost" href="/api/export/contacts.vcf">Export vCard</a>
        <a class="btn ghost" href="#/data?import=contacts">Import</a>
        <button id="new">＋ New contact</button></div></div>
    <div class="toolbar"><input type="search" id="q" placeholder="Filter by name, company, email, phone…" value="${esc(state.q)}">
      <select id="sort"><option value="name">Sort: last name</option><option value="first">Sort: first name</option>
        <option value="company">Sort: company</option><option value="recent">Sort: recently updated</option></select>
      <div id="tag-chips" class="actions"></div></div>
    <div class="split"><div class="card flush"><div class="contact-list" id="list"></div><div class="table-foot" id="count"></div></div>
      <div class="card" id="detail"><div class="empty"><b>Select a contact</b>or create a new one</div></div></div>`;

  const loadTags = async () => {
    const tags = await api("/api/tags");
    $("#tag-chips", el).innerHTML = tags.filter((t) => t.contact_count).map((t) =>
      `<span class="pill tag click ${state.tag === t.name ? "on" : ""}" data-tag="${esc(t.name)}" style="background:${esc(colorFor(t.name))}">${esc(t.name)} ${t.contact_count}</span>`).join("");
  };
  const loadList = async () => {
    const r = await api(`/api/contacts?${qs({ q: state.q, tag: state.tag, sort: state.sort, limit: state.limit })}`);
    $("#list", el).innerHTML = r.items.map((c) => `
      <div class="contact-item ${c.id === state.selected ? "active" : ""}" data-id="${c.id}">${avatar(c)}
        <div class="meta"><div><b>${esc(c.full_name)}</b> ${c.favorite ? "<span style='color:#eab308'>★</span>" : ""}</div>
        <div class="muted small">${esc([c.job_title, c.company].filter(Boolean).join(" · ") || c.email || c.phone)}</div></div></div>`).join("")
      || `<div class="empty"><b>No contacts</b>${state.q || state.tag ? "Nothing matches the filter" : "Add your first contact"}</div>`;
    $("#count", el).innerHTML = `<span>${r.total} contact${r.total === 1 ? "" : "s"}${r.total > r.items.length ? ` · showing ${r.items.length}` : ""}</span>
      ${r.total > r.items.length && state.limit < 500 ? `<button class="ghost small" id="more">Show more</button>` : ""}`;
    $("#more", el)?.addEventListener("click", () => { state.limit = 500; loadList().catch(fail); });
  };
  const showDetail = async (cid) => {
    state.selected = cid;
    $$(".contact-item", el).forEach((x) => x.classList.toggle("active", +x.dataset.id === cid));
    if (location.hash !== `#/contacts/${cid}`) history.replaceState(null, "", `#/contacts/${cid}`);
    const c = await api(`/api/contacts/${cid}`);
    const addr = [c.address, [c.city, c.region, c.postal_code].filter(Boolean).join(" "), c.country].filter(Boolean).join("\n");
    const row = (k, v) => v ? `<dt>${k}</dt><dd>${v}</dd>` : "";
    $("#detail", el).innerHTML = `
      <div class="detail-head">${avatar(c, "lg")}<div style="flex:1"><h1>${esc(c.full_name)} ${c.favorite ? "<span style='color:#eab308'>★</span>" : ""}</h1>
        <div class="muted">${esc([c.job_title, c.company].filter(Boolean).join(" at "))}</div>
        <div class="actions" style="margin-top:6px">${c.tags.map((t) => `<span class="pill tag" style="background:${esc(colorFor(t.name))}">${esc(t.name)}</span>`).join("")}</div></div>
        <img src="/api/contacts/${c.id}/qr.png" alt="vCard QR" title="Scan to add this contact to a phone" style="width:92px;height:92px;border-radius:8px;background:#fff;padding:4px;border:1px solid var(--border)"></div>
      <dl class="kv">
        ${row("Email", c.email && `<a href="mailto:${esc(c.email)}">${esc(c.email)}</a>`)}
        ${row("Phone", c.phone && `<a href="tel:${esc(c.phone)}">${esc(c.phone)}</a>`)}
        ${row("Address", esc(addr))}
        ${row("Birthday", esc(fmtDate(c.birthday)))}
        ${row("Website", c.website && `<a href="${esc(c.website)}" target="_blank" rel="noopener">${esc(c.website)}</a>`)}
        ${row("Notes", esc(c.notes))}
        ${row("Expenses", c.expenses.count ? `<a href="#/expenses?contact_id=${c.id}">${c.expenses.count} linked · ${money(c.expenses.total_cents)}</a>` : "")}
        <dt>Updated</dt><dd class="muted">${esc(new Date(c.updated_at).toLocaleString())}</dd>
      </dl>
      <div class="actions" style="margin-top:18px"><button id="edit">Edit</button>
        <button class="ghost" id="hist">History</button><button class="ghost" id="cfold">Folders…</button>
        <a class="btn ghost" href="/api/contacts/${c.id}/vcard">Download vCard</a>
        <button class="ghost danger" id="del">Delete</button></div>`;
    $("#edit", el).onclick = () => contactForm(c, async (s) => { await loadList(); loadTags(); showDetail(s.id); });
    $("#hist", el).onclick = () => historyModal("contacts", c.id, c.full_name).catch(fail);
    $("#cfold", el).onclick = () => editItemFolders("contact", c.id).catch(fail);
    $("#del", el).onclick = async () => {
      if (!(await confirmBox(`Delete ${c.full_name}? Linked expenses are kept but unlinked.`))) return;
      await api(`/api/contacts/${c.id}`, { method: "DELETE" }).catch(fail);
      toast("Contact deleted");
      state.selected = null;
      history.replaceState(null, "", "#/contacts");
      $("#detail", el).innerHTML = `<div class="empty"><b>Deleted</b></div>`;
      loadList(); loadTags();
    };
  };

  $("#q", el).addEventListener("input", debounce((e) => { state.q = e.target.value; loadList().catch(fail); }));
  $("#sort", el).addEventListener("change", (e) => { state.sort = e.target.value; loadList().catch(fail); });
  $("#tag-chips", el).addEventListener("click", (e) => {
    const t = e.target.closest("[data-tag]");
    if (!t) return;
    state.tag = state.tag === t.dataset.tag ? "" : t.dataset.tag;
    loadTags(); loadList();
  });
  $("#list", el).addEventListener("click", (e) => { const it = e.target.closest("[data-id]"); if (it) showDetail(+it.dataset.id).catch(fail); });
  $("#new", el).onclick = () => contactForm(null, async (s) => { await loadList(); loadTags(); showDetail(s.id); });
  $("#dupes", el).onclick = async () => {
    const d = await api("/api/contacts/duplicates");
    const m = modal({ title: "Possible duplicates", wide: true, body: d.length ? `<p class="muted small">Merging keeps the first contact,
      fills in its blank fields from the others, combines tags, and moves their expenses and section links over. It's recorded in
      History and the merged contacts can be restored from Recently deleted.</p>
      <table><thead><tr><th>Match on</th><th>Value</th><th>Contacts</th><th></th></tr></thead><tbody>
      ${d.map((g) => { const ids = JSON.parse(g.ids).sort((a, b) => a - b); return `<tr><td>${esc(g.match_on)}</td><td>${esc(g.value)}</td>
        <td>${ids.map((i) => `<a href="#/contacts/${i}" data-close>#${i}</a>`).join(", ")}</td>
        <td><button class="ghost small" data-merge="${ids.join(",")}">Merge into #${ids[0]}</button></td></tr>`; }).join("")}
      </tbody></table>` : `<div class="empty"><b>No duplicates</b>No two contacts share an email, phone or name.</div>` });
    m.el.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-merge]"); if (!b) return;
      const [keep, ...rest] = b.dataset.merge.split(",").map(Number);
      if (!(await confirmBox(`Merge #${rest.join(", #")} into #${keep}?`, { ok: "Merge", danger: false }))) return;
      try {
        await api(`/api/contacts/${keep}/merge`, { method: "POST", body: { merge_ids: rest } });
        m.close(); toast("Contacts merged"); await Promise.all([loadList(), loadTags()]); showDetail(keep);
      } catch (err) { fail(err); }
    });
  };
  await Promise.all([loadTags(), loadList()]);
  if (state.selected) showDetail(state.selected).catch(fail);
});

// =============================================================== pictures
async function uploadPictures(files, album = "") {
  const fd = new FormData();
  [...files].forEach((f) => fd.append("file", f));
  if (album) fd.append("album", album);
  const r = await api("/api/pictures", { method: "POST", form: fd });
  const dup = r.items.filter((i) => i.duplicate).length;
  toast(`Uploaded ${r.items.length - dup} picture(s)${dup ? `, ${dup} already stored` : ""}${r.errors.length ? `, ${r.errors.length} rejected` : ""}`,
    r.errors.length ? "error" : "");
  return r;
}

async function pictureViewer(id, onChange) {
  const p = await api(`/api/pictures/${id}`);
  const META = [{ name: "title", label: "Title", full: true }, { name: "album", label: "Album" },
    { name: "taken_at", label: "Taken on", type: "date" }, { name: "description", label: "Description", type: "textarea", full: true }];
  const used = [...p.used_by.contacts.map((c) => `<a href="#/contacts/${c.id}" data-close>${esc(c.first_name)} ${esc(c.last_name)}</a> (photo)`),
    ...p.used_by.expenses.map((e) => `<a href="#/expenses?edit=${e.id}" data-close>${esc(e.merchant || "expense")} ${esc(e.spent_on)}</a> (receipt)`)];
  const m = modal({
    title: p.title || p.original_name, wide: true,
    body: `<div class="lightbox"><a href="/media/${p.id}/view" target="_blank"><img src="/media/${p.id}/view" alt=""></a><div>
      ${formHtml(META, p)}
      <dl class="kv small" style="margin-top:14px;grid-template-columns:90px 1fr">
        <dt>File</dt><dd>${esc(p.original_name)}</dd><dt>Size</dt><dd>${p.width}×${p.height} · ${fmtBytes(p.size_bytes)}</dd>
        <dt>Type</dt><dd>${esc(p.mime)}</dd>${p.camera ? `<dt>Camera</dt><dd>${esc(p.camera)}</dd>` : ""}
        <dt>SHA-256</dt><dd class="mono" style="font-size:10.5px">${esc(p.sha256)}</dd>
        ${used.length ? `<dt>Used by</dt><dd>${used.join("<br>")}</dd>` : ""}</dl></div></div>`,
    foot: `<button class="ghost danger" data-del>Delete</button><a class="btn ghost" href="/media/${p.id}/file?download=1">Download original</a>
      <button class="ghost" data-folders>Folders…</button>
      <span class="spacer"></span><button class="ghost" data-close>Close</button><button data-save>Save</button>`,
  });
  m.$("[data-folders]").onclick = () => editItemFolders("picture", id).catch(fail);
  m.$("[data-save]").onclick = async () => {
    try { await api(`/api/pictures/${id}`, { method: "PATCH", body: readForm(m.el, META) }); m.close(); toast("Saved"); onChange?.(); }
    catch (e) { showErrors(m.el, e); }
  };
  m.$("[data-del]").onclick = async () => {
    if (!(await confirmBox("Delete this picture permanently? Contacts/expenses using it are unlinked."))) return;
    await api(`/api/pictures/${id}`, { method: "DELETE" }).catch(fail);
    m.close(); toast("Picture deleted"); onChange?.();
  };
}

route("/pictures", async (el, params) => {
  const state = { q: "", album: params.album || "" };
  el.innerHTML = `
    <div class="page-head"><div><h1>Pictures</h1><p>Stored once by content hash, with EXIF date & camera extracted.</p></div>
      <div class="actions"><a class="btn ghost" href="/api/export/pictures.csv">Export metadata CSV</a></div></div>
    <label class="dropzone" id="drop"><input type="file" accept="image/*,.heic,.heif" multiple hidden id="files">
      <b>Drop images here</b> or click to browse · JPEG, PNG, WebP, GIF, TIFF up to 40 MB each</label>
    <div class="toolbar"><input type="search" id="q" placeholder="Filter by title, description, file name…">
      <select id="album"><option value="">All albums</option></select><span class="muted small" id="count"></span></div>
    <div class="gallery" id="grid"></div>`;
  const load = async () => {
    const r = await api(`/api/pictures?${qs({ q: state.q, album: state.album, limit: 500 })}`);
    $("#album", el).innerHTML = `<option value="">All albums</option>` + r.albums.map((a) =>
      `<option value="${esc(a.album)}" ${a.album === state.album ? "selected" : ""}>${esc(a.album)} (${a.n})</option>`).join("");
    $("#count", el).textContent = `${r.total} picture${r.total === 1 ? "" : "s"}`;
    $("#grid", el).innerHTML = r.items.map((p) => `<figure data-id="${p.id}"><img loading="lazy" src="/media/${p.id}/thumb" alt="">
      <figcaption><b>${esc(p.title || p.original_name)}</b><div class="muted small">${esc(fmtDate(p.taken_at || p.created_at))}${p.album ? ` · ${esc(p.album)}` : ""}</div></figcaption></figure>`).join("")
      || `<div class="empty" style="grid-column:1/-1"><b>No pictures yet</b>Drop some images above.</div>`;
  };
  const drop = $("#drop", el);
  const doUpload = async (files) => { if (files.length) { await uploadPictures(files, state.album).catch(fail); load(); } };
  $("#files", el).addEventListener("change", (e) => doUpload(e.target.files));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => doUpload(e.dataTransfer.files));
  $("#q", el).addEventListener("input", debounce((e) => { state.q = e.target.value; load(); }));
  $("#album", el).addEventListener("change", (e) => { state.album = e.target.value; load(); });
  $("#grid", el).addEventListener("click", (e) => { const f = e.target.closest("[data-id]"); if (f) pictureViewer(+f.dataset.id, load).catch(fail); });
  await load();
  if (params.open) pictureViewer(+params.open, load).catch(fail);
});

// =============================================================== expenses
async function expenseForm(existing, onSaved, preset = {}) {
  const [cats, contacts] = await Promise.all([api("/api/categories"), api("/api/contacts?limit=500")]);
  const FIELDS = [
    { name: "spent_on", label: "Date", type: "date", required: true, default: today() },
    { name: "amount", label: "Amount", required: true, placeholder: "0.00", type: "text" },
    { name: "merchant", label: "Merchant", list: "merchant-list" },
    { name: "category_id", label: "Category", type: "select", options: [["", "— none —"], ...cats.map((c) => [c.id, c.name])] },
    { name: "payment_method", label: "Payment method", list: "pm-list" },
    { name: "currency", label: "Currency", default: "USD" },
    { name: "contact_id", label: "Paid to / with (contact)", type: "select", full: true,
      options: [["", "— none —"], ...contacts.items.map((c) => [c.id, c.full_name])] },
    { name: "description", label: "Description", type: "textarea", full: true },
  ];
  const values = existing ? { ...existing, amount: (existing.amount_cents / 100).toFixed(2) } : preset;
  let receipt = existing?.receipt_picture_id ?? null;
  const m = modal({
    title: existing ? "Edit expense" : "New expense",
    body: `<datalist id="merchant-list"></datalist><datalist id="pm-list"><option value="Visa"><option value="Mastercard"><option value="Debit"><option value="Cash"><option value="Apple Pay"></datalist>
      ${formHtml(FIELDS, values)}
      <div style="margin-top:14px;display:flex;gap:12px;align-items:center"><div id="rc"></div>
        <label class="btn ghost small">🧾 Attach receipt<input type="file" accept="image/*,.heic,.heif" hidden id="rc-in"></label></div>`,
    foot: `${existing ? `<button class="ghost danger" data-del>Delete</button><button class="ghost" data-hist>History</button><button class="ghost" data-folders>Folders…</button><span class="spacer"></span>` : ""}
      <button class="ghost" data-close>Cancel</button><button data-save>${existing ? "Save" : "Add expense"}</button>`,
  });
  const showReceipt = () => { m.$("#rc").innerHTML = receipt ? `<a href="/media/${receipt}/file" target="_blank"><img src="/media/${receipt}/thumb" style="height:56px;border-radius:6px"></a>
      <button class="icon-btn" id="rc-x" title="Detach">✕</button>` : "";
    m.$("#rc-x")?.addEventListener("click", () => { receipt = null; showReceipt(); }); };
  showReceipt();
  api("/api/query", { method: "POST", body: { sql: "SELECT merchant FROM expenses WHERE merchant <> '' GROUP BY merchant ORDER BY COUNT(*) DESC LIMIT 50" } })
    .then((r) => { m.$("#merchant-list").innerHTML = r.rows.map(([x]) => `<option value="${esc(x)}">`).join(""); }).catch(() => {});
  m.$("#rc-in").addEventListener("change", async (e) => {
    if (!e.target.files[0]) return;
    try { const r = await uploadPictures(e.target.files, "Receipts"); receipt = r.items[0]?.id ?? receipt; showReceipt(); } catch (err) { fail(err); }
  });
  m.$("[data-save]").onclick = async () => {
    const data = { ...readForm(m.el, FIELDS), receipt_picture_id: receipt };
    try {
      const saved = existing ? await api(`/api/expenses/${existing.id}`, { method: "PATCH", body: data })
        : await api("/api/expenses", { method: "POST", body: data });
      m.close(); toast(existing ? "Expense updated" : `Added ${money(saved.amount_cents)}`); onSaved(saved);
    } catch (e) { showErrors(m.el, e); }
  };
  m.$("[data-del]")?.addEventListener("click", async () => {
    if (!(await confirmBox("Delete this expense?"))) return;
    await api(`/api/expenses/${existing.id}`, { method: "DELETE" }).catch(fail);
    m.close(); toast("Expense deleted — undo from Data › Recently deleted"); onSaved(null);
  });
  m.$("[data-hist]")?.addEventListener("click", () => historyModal("expenses", existing.id, existing.merchant || "expense").catch(fail));
  m.$("[data-folders]")?.addEventListener("click", () => editItemFolders("expense", existing.id).catch(fail));
}

function categoryManager(onChange) {
  const m = modal({ title: "Categories & budgets", wide: true, body: `<div id="cats"></div>
      <h3 style="margin-top:18px">Add category</h3>
      <div class="toolbar"><input id="cn" placeholder="Name"><input id="cc" type="color" value="#4f46e5"><input id="cb" placeholder="Monthly budget (optional)"><button id="ca">Add</button></div>`,
  foot: `<button data-close>Done</button>` });
  const load = async () => {
    const cats = await api("/api/categories");
    m.$("#cats").innerHTML = `<table><thead><tr><th></th><th>Name</th><th>Monthly budget</th><th class="right">Expenses</th><th></th></tr></thead><tbody>
      ${cats.map((c) => `<tr data-id="${c.id}"><td><input type="color" value="${esc(c.color)}" data-k="color"></td>
        <td><input value="${esc(c.name)}" data-k="name"></td>
        <td><input value="${c.monthly_budget_cents != null ? (c.monthly_budget_cents / 100).toFixed(2) : ""}" placeholder="none" data-k="monthly_budget"></td>
        <td class="num">${c.expense_count}</td><td><button class="icon-btn" data-del title="Delete">🗑</button></td></tr>`).join("")}</tbody></table>`;
  };
  m.el.addEventListener("change", async (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr || !e.target.dataset.k) return;
    try { await api(`/api/categories/${tr.dataset.id}`, { method: "PATCH", body: { [e.target.dataset.k]: e.target.value } }); toast("Saved"); onChange(); }
    catch (err) { fail(err); load(); }
  });
  m.el.addEventListener("click", async (e) => {
    const tr = e.target.closest("[data-del]")?.closest("tr");
    if (!tr) return;
    if (!(await confirmBox("Delete category? Its expenses become uncategorized."))) return;
    await api(`/api/categories/${tr.dataset.id}`, { method: "DELETE" }).catch(fail);
    load(); onChange();
  });
  m.$("#ca").onclick = async () => {
    try {
      await api("/api/categories", { method: "POST", body: { name: m.$("#cn").value, color: m.$("#cc").value, monthly_budget: m.$("#cb").value } });
      m.$("#cn").value = m.$("#cb").value = ""; load(); onChange();
    } catch (e) { fail(e); }
  };
  load();
}

// column -> [first-click sort, second-click sort] (keys of EXPENSE_SORTS on the server)
const EXPENSE_SORT = { date: ["date", "date_asc"], amount: ["amount", "amount_asc"], merchant: ["merchant", "merchant_desc"] };

route("/expenses", async (el, params) => {
  const now = new Date();
  const state = { q: "", category_id: "", date_from: "", date_to: "", min_amount: "", max_amount: "", contact_id: params.contact_id || "",
    sort: "date", offset: 0, limit: 100, month: `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}` };
  const cats = await api("/api/categories");
  el.innerHTML = `
    <div class="page-head"><div><h1>Expenses</h1><p>Stored as integer cents — no floating-point drift.</p></div>
      <div class="actions"><button class="ghost" id="cats">Categories & budgets</button>
        <a class="btn ghost" href="#/data?import=expenses">Import CSV</a><a class="btn ghost" href="/api/export/expenses.csv">Export CSV</a>
        <button id="new">＋ Add expense</button></div></div>
    <div class="grid span" style="margin-bottom:16px">
      <div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><h3>12-month trend</h3><span class="muted small">click a bar to filter</span></div><div id="trend"></div></div>
      <div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><h3 style="margin:0">Budget</h3>
        <input type="month" id="month" value="${state.month}" style="padding:4px 8px"></div><div id="budget"></div></div></div>
    <div class="card" id="recurring-card" style="margin-bottom:16px" hidden><div style="display:flex;justify-content:space-between;align-items:center">
      <h3 style="margin:0">Recurring charges</h3><span class="muted small" id="rec-total"></span></div>
      <p class="muted small" style="margin:6px 0 10px">Same merchant, about the same amount, month after month — detected with a SQL window function (<code>LAG</code>).</p>
      <div class="table-wrap"><table><thead><tr><th>Merchant</th><th class="right">Typical</th><th class="right">Months</th><th>Last charge</th><th>Next expected</th><th class="right">Per year</th></tr></thead>
      <tbody id="rec-rows"></tbody></table></div></div>
    <div class="toolbar"><input type="search" id="q" placeholder="Merchant, description, method…">
      <select id="cat"><option value="">All categories</option>${cats.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}<option value="none">Uncategorized</option></select>
      <input type="date" id="df" title="From"><input type="date" id="dt" title="To">
      <input id="mn" placeholder="Min $" style="width:90px"><input id="mx" placeholder="Max $" style="width:90px">
      <button class="ghost small" id="clear">Clear</button></div>
    ${state.contact_id ? `<div class="toolbar"><span class="pill">Filtered to contact #${esc(state.contact_id)} <a href="#/expenses">✕</a></span></div>` : ""}
    <div class="card flush"><div class="table-wrap"><table><thead><tr>
      <th class="sortable" data-sort="date">Date</th><th class="sortable" data-sort="merchant">Merchant</th><th>Category</th><th>Method</th>
      <th>Description</th><th></th><th class="sortable right" data-sort="amount">Amount</th></tr></thead><tbody id="rows"></tbody></table></div>
      <div class="table-foot"><span id="sum"></span><span class="actions"><button class="ghost small" id="prev">← Prev</button><button class="ghost small" id="next">Next →</button></span></div></div>`;

  const loadSummary = async () => {
    const s = await api(`/api/expenses/summary?month=${state.month}`);
    $("#trend", el).innerHTML = barChart(s.trend, { value: (d) => d.total_cents, label: (d) => monthLabel(d.month), fmt: shortMoney });
    $$("#trend rect", el).forEach((r, i) => r.addEventListener("click", () => {
      const m = s.trend[i].month, [y, mo] = m.split("-").map(Number);
      $("#df", el).value = state.date_from = `${m}-01`;
      $("#dt", el).value = state.date_to = new Date(y, mo, 0).toLocaleDateString("en-CA");
      state.offset = 0; loadRows();
    }));
    $("#budget", el).innerHTML = budgetBars(s.by_category);
  };
  const loadRows = async () => {
    const r = await api(`/api/expenses?${qs({ ...state, month: "" })}`);
    $("#rows", el).innerHTML = r.items.map((x) => `<tr class="clickable" data-id="${x.id}">
      <td class="nowrap">${esc(fmtDate(x.spent_on))}</td><td><b>${esc(x.merchant || "—")}</b>${x.contact_name ? `<div class="muted small">${esc(x.contact_name)}</div>` : ""}</td>
      <td>${x.category ? `<span class="pill"><span class="dot" style="background:${esc(x.category_color)}"></span>${esc(x.category)}</span>` : `<span class="muted">—</span>`}</td>
      <td class="muted">${esc(x.payment_method)}</td><td class="muted" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.description)}</td>
      <td>${x.receipt_picture_id ? `<img class="thumb" src="/media/${x.receipt_picture_id}/thumb" title="Receipt">` : ""}</td>
      <td class="num"><b>${money(x.amount_cents, x.currency)}</b></td></tr>`).join("")
      || `<tr><td colspan="7"><div class="empty"><b>No expenses</b>Nothing matches these filters.</div></td></tr>`;
    const end = Math.min(r.offset + r.items.length, r.total);
    $("#sum", el).innerHTML = `${r.total ? `${r.offset + 1}–${end} of ${r.total}` : "0"} · total <b>${money(r.sum_cents)}</b>`;
    $("#prev", el).disabled = r.offset === 0;
    $("#next", el).disabled = end >= r.total;
    $$("th.sortable", el).forEach((th) => {
      const [first, second] = EXPENSE_SORT[th.dataset.sort];
      const arrow = state.sort === first ? (th.dataset.sort === "merchant" ? " ▲" : " ▼") : state.sort === second ? (th.dataset.sort === "merchant" ? " ▼" : " ▲") : "";
      th.textContent = th.textContent.replace(/ [▲▼]$/, "") + arrow;
    });
  };
  const loadRecurring = async () => {
    const r = await api("/api/expenses/recurring");
    $("#recurring-card", el).hidden = !r.items.length;
    $("#rec-total", el).textContent = `${money(r.monthly_cents)}/month · ${money(r.yearly_cents)}/year`;
    $("#rec-rows", el).innerHTML = r.items.map((x) => `<tr class="clickable" data-merchant="${esc(x.merchant)}"><td><b>${esc(x.merchant)}</b></td>
      <td class="num">${money(x.avg_cents)}</td><td class="num">${x.months_charged}</td><td>${esc(fmtDate(x.last_charge))}</td>
      <td>${esc(fmtDate(x.next_expected))}</td><td class="num">${money(x.yearly_cents)}</td></tr>`).join("");
  };
  $("#rec-rows", el).addEventListener("click", (e) => {
    const tr = e.target.closest("[data-merchant]"); if (!tr) return;
    $("#q", el).value = state.q = tr.dataset.merchant; state.offset = 0; loadRows().catch(fail);
  });
  const refresh = () => Promise.all([loadRows(), loadSummary(), loadRecurring()]).catch(fail);
  const bind = (sel, key) => $(sel, el).addEventListener("input", debounce((e) => { state[key] = e.target.value; state.offset = 0; loadRows().catch(fail); }, 300));
  bind("#q", "q"); bind("#cat", "category_id"); bind("#df", "date_from"); bind("#dt", "date_to"); bind("#mn", "min_amount"); bind("#mx", "max_amount");
  $("#month", el).addEventListener("change", (e) => { state.month = e.target.value; loadSummary().catch(fail); });
  $("#clear", el).onclick = () => { ["#q", "#cat", "#df", "#dt", "#mn", "#mx"].forEach((s) => ($(s, el).value = ""));
    Object.assign(state, { q: "", category_id: "", date_from: "", date_to: "", min_amount: "", max_amount: "", offset: 0 }); loadRows(); };
  $("#prev", el).onclick = () => { state.offset = Math.max(0, state.offset - state.limit); loadRows(); };
  $("#next", el).onclick = () => { state.offset += state.limit; loadRows(); };
  $$("th.sortable", el).forEach((th) => th.addEventListener("click", () => {
    const [first, second] = EXPENSE_SORT[th.dataset.sort];  // click toggles between the two directions
    state.sort = state.sort === first ? second : first;
    state.offset = 0; loadRows().catch(fail);
  }));
  $("#rows", el).addEventListener("click", async (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (tr) expenseForm(await api(`/api/expenses/${tr.dataset.id}`), refresh).catch(fail);
  });
  $("#new", el).onclick = () => expenseForm(null, refresh).catch(fail);
  $("#cats", el).onclick = () => categoryManager(refresh);
  await refresh();
  if (params.new) expenseForm(null, refresh).catch(fail);
  if (params.edit) expenseForm(await api(`/api/expenses/${params.edit}`), refresh).catch(fail);
});

// ================================================================== codes
let KINDS = null;
const kindHints = { qr: "Any text, URL, vCard… (up to ~2.9 KB)", ean13: "12 digits (check digit added) or 13", ean8: "7 or 8 digits",
  upca: "11 digits (check digit added) or 12", code128: "Printable ASCII, e.g. INV-0042", code39: "A–Z, 0–9, space - . $ / + %", isbn13: "978/979 + 9 digits, or full 13" };

route("/codes", async (el) => {
  KINDS = KINDS || await api("/api/codes/kinds");
  const state = { q: "", kind: "", stream: null, timer: null };
  el.innerHTML = `
    <div class="page-head"><div><h1>Barcodes & QR codes</h1><p>Generate, scan from photos or your webcam, and keep a searchable library.</p></div>
      <div class="actions"><a class="btn ghost" href="#/data?import=codes">Import CSV</a><a class="btn ghost" href="/api/export/codes.csv">Export CSV</a></div></div>
    <div class="grid cols-2" style="margin-bottom:18px">
      <div class="card"><h2>Generate</h2>
        <div class="seg" style="margin-bottom:12px"><button class="on" data-mode="text">Text / number</button><button data-mode="wifi">Wi-Fi QR</button></div>
        <div id="gen-text" class="form-grid">
          <label class="field"><span>Type</span><select id="kind">${Object.entries(KINDS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></label>
          <label class="field"><span>Label</span><input id="label" placeholder="What is this?"></label>
          <label class="field full"><span>Payload</span><textarea id="payload" rows="2" placeholder="https://…"></textarea><span class="muted small" id="hint" style="font-weight:400"></span></label></div>
        <div id="gen-wifi" class="form-grid" hidden>
          <label class="field"><span>Network name (SSID)</span><input id="ssid"></label><label class="field"><span>Password</span><input id="wpass"></label>
          <label class="field"><span>Security</span><select id="wsec"><option>WPA</option><option>WEP</option><option value="nopass">None</option></select></label>
          <label class="field"><span>Label</span><input id="wlabel" placeholder="Home Wi-Fi"></label></div>
        <div class="preview-box" style="margin-top:12px" id="preview"><span class="muted">Live preview</span></div>
        <div class="actions" style="margin-top:12px"><button id="save">Save to library</button><span class="field-error" id="gen-err"></span></div></div>
      <div class="card"><h2>Scan</h2>
        <label class="dropzone" id="scan-drop"><input type="file" accept="image/*,.heic,.heif" hidden id="scan-file"><b>Drop a photo of a barcode / QR</b> or click — every code in the image is read</label>
        <div class="actions" style="margin-bottom:10px"><button class="ghost" id="cam">📷 Use webcam</button>
          <label class="check"><input type="checkbox" id="autosave" checked> Save scans to library</label></div>
        <video class="scanner" id="video" playsinline muted hidden></video>
        <div id="scan-out"></div></div></div>
    <div class="toolbar"><input type="search" id="q" placeholder="Search payloads and labels…">
      <select id="fkind"><option value="">All types</option>${Object.entries(KINDS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
      <span class="muted small" id="count"></span></div>
    <div class="grid cols-4" id="lib"></div>`;

  let mode = "text";
  const genPayload = () => mode === "wifi" ? null : { kind: $("#kind", el).value, payload: $("#payload", el).value };
  const preview = debounce(async () => {
    const box = $("#preview", el), errEl = $("#gen-err", el);
    errEl.textContent = "";
    let url;
    if (mode === "wifi") {
      const ssid = $("#ssid", el).value;
      if (!ssid) { box.innerHTML = `<span class="muted">Enter a network name</span>`; return; }
      const esc2 = (s) => s.replace(/([\\;,:"])/g, "\\$1");
      url = `/api/codes/preview?${qs({ kind: "qr", payload: `WIFI:T:${$("#wsec", el).value};S:${esc2(ssid)};P:${esc2($("#wpass", el).value)};H:false;;` })}`;
    } else {
      const g = genPayload();
      $("#hint", el).textContent = kindHints[g.kind];
      if (!g.payload.trim()) { box.innerHTML = `<span class="muted">Live preview</span>`; return; }
      url = `/api/codes/preview?${qs(g)}`;
    }
    const res = await fetch(url);
    if (!res.ok) { const j = await res.json(); errEl.textContent = Object.values(j.fields || {}).join("; ") || j.message; box.innerHTML = ""; return; }
    const blob = await res.blob();
    box.innerHTML = `<img src="${URL.createObjectURL(blob)}" alt="preview">`;
  }, 250);
  $$("[data-mode]", el).forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    $$("[data-mode]", el).forEach((x) => x.classList.toggle("on", x === b));
    $("#gen-text", el).hidden = mode !== "text"; $("#gen-wifi", el).hidden = mode !== "wifi"; preview();
  }));
  ["#kind", "#payload", "#ssid", "#wpass", "#wsec"].forEach((s) => $(s, el).addEventListener("input", preview));
  preview();

  const loadLib = async () => {
    const r = await api(`/api/codes?${qs({ q: state.q, kind: state.kind, limit: 500 })}`);
    $("#count", el).textContent = `${r.total} code${r.total === 1 ? "" : "s"}`;
    $("#lib", el).innerHTML = r.items.map((c) => {
      const info = c.info.type === "url" ? `<a href="${esc(c.payload)}" target="_blank" rel="noopener">open link ↗</a>` :
        c.info.type === "wifi" ? `Wi-Fi · ${esc(c.info.ssid)}` : c.info.type === "book" ? "Book (ISBN)" : c.info.type === "product" ? "Product (GTIN)" : "";
      return `<div class="card code-card" data-id="${c.id}"><div class="img"><img loading="lazy" src="/api/codes/${c.id}/image.png" alt=""></div>
        <div style="display:flex;justify-content:space-between;gap:6px"><b style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.label || c.kind_label)}</b>
          <span class="pill">${esc(c.kind_label)}</span></div>
        <div class="payload">${esc(c.payload)}</div>
        <div class="muted small">${info}${c.scan_count ? ` · scanned ${c.scan_count}×` : ""} · ${esc(c.source)}</div>
        <div class="actions"><a class="btn ghost small" href="/api/codes/${c.id}/image.png?download=1">PNG</a><a class="btn ghost small" href="/api/codes/${c.id}/image.svg?download=1">SVG</a>
          <button class="ghost small" data-act="copy">Copy</button><button class="ghost small" data-act="edit">Edit</button>
          <button class="ghost small" data-act="folders" title="Folders">📁</button><button class="icon-btn" data-act="del" title="Delete">🗑</button></div></div>`;
    }).join("") || `<div class="card empty" style="grid-column:1/-1"><b>Library is empty</b>Generate or scan a code above.</div>`;
  };
  $("#save", el).onclick = async () => {
    const body = mode === "wifi" ? { label: $("#wlabel", el).value || $("#ssid", el).value,
      wifi: { ssid: $("#ssid", el).value, password: $("#wpass", el).value, security: $("#wsec", el).value } }
      : { ...genPayload(), label: $("#label", el).value };
    try { const res = await fetch("/api/codes", { method: "POST", headers: { "Content-Type": "application/json", "X-DataVault": "1" }, body: JSON.stringify(body) });
      const j = await res.json(); if (!res.ok) throw new ApiError(res.status, j);
      toast(res.status === 201 ? "Saved to library" : "Already in library"); loadLib(); }
    catch (e) { $("#gen-err", el).textContent = Object.values(e.fields || {}).join("; ") || e.message; }
  };
  $("#lib", el).addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const id = b.closest("[data-id]").dataset.id;
    if (b.dataset.act === "folders") return editItemFolders("code", +id).catch(fail);
    if (b.dataset.act === "copy") { const c = await api(`/api/codes/${id}`); await navigator.clipboard.writeText(c.payload); toast("Payload copied"); }
    if (b.dataset.act === "del" && await confirmBox("Delete this code?")) { await api(`/api/codes/${id}`, { method: "DELETE" }); loadLib(); }
    if (b.dataset.act === "edit") {
      const c = await api(`/api/codes/${id}`);
      const F = [{ name: "label", label: "Label", full: true }, { name: "notes", label: "Notes", type: "textarea", full: true }];
      const m = modal({ title: `${c.kind_label}: ${c.payload.slice(0, 40)}`, body: formHtml(F, c), foot: `<button class="ghost" data-close>Cancel</button><button data-save>Save</button>` });
      m.$("[data-save]").onclick = async () => { await api(`/api/codes/${id}`, { method: "PATCH", body: readForm(m.el, F) }).catch(fail); m.close(); loadLib(); };
    }
  });
  $("#q", el).addEventListener("input", debounce((e) => { state.q = e.target.value; loadLib(); }));
  $("#fkind", el).addEventListener("change", (e) => { state.kind = e.target.value; loadLib(); });

  // --- scanning
  const showFound = (r, quiet = false) => {
    if (!r.found.length) { if (!quiet) $("#scan-out", el).innerHTML = `<div class="error-box">No barcode or QR code found. Try a sharper, closer, well-lit photo.</div>`; return false; }
    $("#scan-out", el).innerHTML = r.found.map((f) => `<div class="list-row"><span class="pill">${esc(f.symbology)}</span>
      <span class="mono" style="flex:1;word-break:break-all">${esc(f.payload)}</span>${f.info.type === "url" ? `<a href="${esc(f.payload)}" target="_blank" rel="noopener">open ↗</a>` : ""}</div>`).join("")
      + (r.saved.length ? `<div class="muted small" style="margin-top:6px">Saved to library ✓</div>` : "");
    if (r.saved.length) loadLib();
    return true;
  };
  const scanBlob = async (blob, quiet) => {
    const fd = new FormData(); fd.append("file", blob, "scan.jpg"); fd.append("save", $("#autosave", el).checked ? "1" : "0");
    return showFound(await api("/api/codes/decode", { method: "POST", form: fd }), quiet);
  };
  $("#scan-file", el).addEventListener("change", (e) => e.target.files[0] && scanBlob(e.target.files[0]).catch(fail));
  const sd = $("#scan-drop", el);
  ["dragenter", "dragover"].forEach((ev) => sd.addEventListener(ev, (e) => { e.preventDefault(); sd.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => sd.addEventListener(ev, (e) => { e.preventDefault(); sd.classList.remove("over"); }));
  sd.addEventListener("drop", (e) => e.dataTransfer.files[0] && scanBlob(e.dataTransfer.files[0]).catch(fail));
  const stopCam = () => { clearInterval(state.timer); state.stream?.getTracks().forEach((t) => t.stop()); state.stream = null;
    const v = $("#video", el); if (v) v.hidden = true; const b = $("#cam", el); if (b) b.textContent = "📷 Use webcam"; };
  $("#cam", el).onclick = async () => {
    if (state.stream) return stopCam();
    try { state.stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: 1280 } }); }
    catch (e) { return toast(`Camera unavailable: ${e.message}`, "error"); }
    const v = $("#video", el); v.srcObject = state.stream; v.hidden = false; await v.play();
    $("#cam", el).textContent = "■ Stop webcam";
    $("#scan-out", el).innerHTML = `<div class="muted small">Hold a code up to the camera…</div>`;
    const canvas = document.createElement("canvas");
    let busy = false;
    state.timer = setInterval(async () => {
      if (busy || !v.videoWidth) return;
      busy = true;
      canvas.width = v.videoWidth; canvas.height = v.videoHeight;
      canvas.getContext("2d").drawImage(v, 0, 0);
      const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.85));
      try { if (await scanBlob(blob, true)) { stopCam(); toast("Code detected"); } } catch (e) { /* keep trying */ }
      busy = false;
    }, 700);
  };
  await loadLib();
  return stopCam;
});

// =============================================================== sections
const FIELD_TYPES = { text: "Text", longtext: "Long text", number: "Number", money: "Money", date: "Date", boolean: "Yes / No", select: "Choice list",
  url: "Link", email: "Email", picture: "Picture", contact: "Contact", code: "Barcode / QR", formula: "ƒ Formula" };
const RESULT_TYPES = { number: "→ Number", money: "→ Money", text: "→ Text", date: "→ Date", boolean: "→ Yes / No" };
const RESERVED = new Set(["id", "section_id", "created_at", "updated_at"]);
// mirrors validation.field_key on the server, so the builder can show the keys formulas use
const fieldKey = (s) => {
  let k = String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  if (!k || /^[0-9]/.test(k)) k = "f_" + k;
  return RESERVED.has(k) ? "f_" + k : k;
};
const resultType = (f) => (f.type === "formula" ? f.options?.result || "number" : f.type);
const isNumeric = (f) => ["money", "number"].includes(resultType(f));
let FORMULA_FUNCS = null;

async function loadSectionNav() {
  const secs = await api("/api/sections");
  $("#section-nav").innerHTML = secs.map((s) => `<a href="#/s/${esc(s.slug)}" data-nav="s-${esc(s.slug)}"><i>${esc(s.icon)}</i>${esc(s.name)}
    <span class="muted small" style="margin-left:auto">${s.record_count}</span></a>`).join("")
    || `<div class="muted small" style="padding:4px 10px">No custom sections yet</div>`;
  const cur = location.hash;
  $$("#section-nav a").forEach((a) => a.classList.toggle("active", cur.startsWith(a.getAttribute("href"))));
}

async function sectionBuilder(existing) {
  FORMULA_FUNCS = FORMULA_FUNCS || await api("/api/formula/functions").catch(() => []);
  const fields = existing ? existing.fields.map((f) => f.type === "formula"
    ? { key: f.key, label: f.label, type: "formula", required: false, options: "", formula: f.options.expr, result: f.options.result }
    : { key: f.key, label: f.label, type: f.type, required: f.required, options: f.options.join(", ") })
    : [{ label: "Name", type: "text", required: true, options: "" }];
  const m = modal({
    title: existing ? `Edit “${existing.name}”` : "New section", wide: true,
    body: `<div class="form-grid" style="grid-template-columns:80px 1fr">
        <label class="field"><span>Icon</span><input id="s-icon" value="${esc(existing?.icon || "📁")}" maxlength="4" style="text-align:center;font-size:18px"></label>
        <label class="field"><span class="req">Name</span><input id="s-name" value="${esc(existing?.name || "")}" placeholder="e.g. Recipes, Vehicles, Warranties"></label>
        <label class="field full"><span>Description</span><input id="s-desc" value="${esc(existing?.description || "")}"></label></div>
      <h3 style="margin-top:18px">Fields <span class="muted small" style="text-transform:none;letter-spacing:0">— drag to reorder</span></h3>
      <div class="field-rows" id="rows"></div>
      <div class="actions" style="margin-top:10px"><button class="ghost small" id="add">＋ Add field</button>
        <button class="ghost small" id="add-fx">ƒ Add formula</button></div>
      <details class="formula-help" id="fx-help" ${fields.some((f) => f.type === "formula") ? "open" : ""}>
        <summary>Formula help</summary>
        <p class="small">Use field names like <code>price * quantity</code>. Spreadsheet style works too:
          <code>=IF(status = "Owned", price, 0)</code>, <code>first &amp; " " &amp; last</code>, <code>total ^ 2</code>, <code>&lt;&gt;</code>.
          Blank inputs give a blank result; dividing by zero gives a blank.</p>
        <div class="small"><b>Fields:</b> <span id="fx-keys"></span></div>
        <div class="fx-funcs small">${(FORMULA_FUNCS || []).map((f) => `<code title="${esc(f.help)}" data-fn="${esc(f.name)}">${esc(f.help)}</code>`).join("")}</div>
      </details>
      ${existing ? `<p class="muted small">Renaming a field label keeps its data. Removing a field hides its values (they stay in the stored JSON).
        Formulas are recalculated for every record when you save.</p>` : ""}`,
    foot: `${existing ? `<button class="ghost danger" data-del>Delete section</button><span class="spacer"></span>` : ""}
      <button class="ghost" data-close>Cancel</button><button data-save>${existing ? "Save" : "Create section"}</button>`,
  });
  let lastFocusedFx = null;
  const keysNow = () => fields.map((f) => f.key || fieldKey(f.label));
  const renderKeys = () => {
    m.$("#fx-keys").innerHTML = fields.map((f, i) => f.label ? `<code class="click" data-key="${esc(keysNow()[i])}" title="${esc(f.label)}">${esc(keysNow()[i])}</code>` : "").join(" ");
  };
  const render = () => {
    m.$("#rows").innerHTML = fields.map((f, i) => {
      const fx = f.type === "formula";
      return `<div class="field-row ${fx ? "fx" : ""}" draggable="true" data-i="${i}"><span class="grip">⋮⋮</span>
      <input data-k="label" value="${esc(f.label)}" placeholder="Field label">
      <select data-k="type">${Object.entries(FIELD_TYPES).map(([k, v]) => `<option value="${k}" ${f.type === k ? "selected" : ""}>${v}</option>`).join("")}</select>
      ${fx ? `<input data-k="formula" class="mono" value="${esc(f.formula || "")}" placeholder="e.g. price * quantity" spellcheck="false">
        <select data-k="result">${Object.entries(RESULT_TYPES).map(([k, v]) => `<option value="${k}" ${(f.result || "number") === k ? "selected" : ""}>${v}</option>`).join("")}</select>`
      : `<input data-k="options" value="${esc(f.options || "")}" placeholder="${f.type === "select" ? "Choices, comma-separated" : "—"}" ${f.type === "select" ? "" : "disabled"}>
        <label class="check small"><input type="checkbox" data-k="required" ${f.required ? "checked" : ""}> Required</label>`}
      <button class="icon-btn" data-rm title="Remove">✕</button>
      ${fx ? `<div class="fx-msg small" data-msg="${i}"></div>` : ""}</div>`;
    }).join("");
    renderKeys();
    checkFormulas();
  };
  // live validation + sample evaluation for one formula row
  const checkOne = async (i) => {
    const f = fields[i], slot = m.$(`[data-msg="${i}"]`);
    if (!f || f.type !== "formula" || !slot) return;
    if (!(f.formula || "").trim()) { slot.className = "fx-msg small muted"; slot.textContent = "Type a formula"; return; }
    const sample = {};
    fields.forEach((o, j) => { if (j !== i && isNumeric(o) && o.type !== "formula") sample[keysNow()[j]] = 10; });
    try {
      const r = await api("/api/formula/check", { method: "POST", body: {
        fields: fields.map((o, j) => ({ key: keysNow()[j], label: o.label })), key: keysNow()[i], expr: f.formula, result: f.result || "number", sample } });
      slot.className = `fx-msg small ${r.ok ? "ok" : "bad"}`;
      slot.textContent = r.ok ? `✓ valid${r.refs.length ? ` · uses ${r.refs.join(", ")}` : ""}${r.volatile ? " · refreshes daily" : ""}`
        + (Object.keys(sample).length && r.value != null
          ? ` · if every number were 10 → ${(f.result || "number") === "money" ? money(r.value) : r.value}` : "") : `✗ ${r.error}`;
    } catch (e) { slot.className = "fx-msg small bad"; slot.textContent = e.message; }
  };
  // one shared debounce that re-checks *every* formula row (a per-call debounce would drop all but the last row)
  const checkFormulas = debounce(() => fields.forEach((f, i) => { if (f.type === "formula") checkOne(i); }), 300);
  render();
  m.$("#rows").addEventListener("input", (e) => {
    const row = e.target.closest("[data-i]"), k = e.target.dataset.k; if (!row || !k) return;
    const i = +row.dataset.i, f = fields[i];
    f[k] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    if (k === "type") { if (f.type === "formula") { f.required = false; f.result = f.result || "number"; } render(); }
    else if (k === "label") { renderKeys(); checkFormulas(); }
    else if (k === "formula" || k === "result") checkFormulas();
  });
  m.$("#rows").addEventListener("focusin", (e) => { if (e.target.dataset.k === "formula") lastFocusedFx = e.target; });
  // clicking a field key or function in the help panel inserts it into the formula being edited
  m.$("#fx-help").addEventListener("click", (e) => {
    const k = e.target.closest("[data-key]")?.dataset.key || (e.target.closest("[data-fn]") ? `${e.target.closest("[data-fn]").dataset.fn}(` : null);
    if (!k || !lastFocusedFx) return;
    lastFocusedFx.setRangeText(k, lastFocusedFx.selectionStart, lastFocusedFx.selectionEnd, "end");
    lastFocusedFx.dispatchEvent(new Event("input", { bubbles: true }));
    lastFocusedFx.focus();
  });
  m.$("#rows").addEventListener("click", (e) => { if (e.target.closest("[data-rm]")) { fields.splice(e.target.closest("[data-i]").dataset.i, 1); render(); } });
  let dragFrom = null;
  m.$("#rows").addEventListener("dragstart", (e) => {
    if (e.target.matches?.("input, select")) return;
    dragFrom = +e.target.closest("[data-i]").dataset.i; e.target.classList.add("dragging");
  });
  m.$("#rows").addEventListener("dragover", (e) => e.preventDefault());
  m.$("#rows").addEventListener("drop", (e) => {
    e.preventDefault(); const to = e.target.closest("[data-i]"); if (!to || dragFrom == null) return;
    const [f] = fields.splice(dragFrom, 1); fields.splice(+to.dataset.i, 0, f); dragFrom = null; render();
  });
  m.$("#add").onclick = () => { fields.push({ label: "", type: "text", required: false, options: "" }); render(); $$("[data-k=label]", m.el).at(-1).focus(); };
  m.$("#add-fx").onclick = () => {
    fields.push({ label: "", type: "formula", required: false, options: "", formula: "", result: "number" });
    m.$("#fx-help").open = true; render(); $$("[data-k=label]", m.el).at(-1).focus();
  };
  m.$("[data-save]").onclick = async () => {
    const body = { name: m.$("#s-name").value, icon: m.$("#s-icon").value, description: m.$("#s-desc").value, fields };
    try {
      const s = existing ? await api(`/api/sections/${existing.id}`, { method: "PUT", body }) : await api("/api/sections", { method: "POST", body });
      m.close(); toast(existing ? "Section updated" : "Section created"); await loadSectionNav();
      if (location.hash === `#/s/${s.slug}`) navigate(); else location.hash = `#/s/${s.slug}`;
    } catch (e) { fail(e); }
  };
  m.$("[data-del]")?.addEventListener("click", async () => {
    if (!(await confirmBox(`Delete “${existing.name}” and all ${existing.record_count} records? This cannot be undone.`))) return;
    await api(`/api/sections/${existing.id}`, { method: "DELETE" }).catch(fail);
    m.close(); await loadSectionNav(); location.hash = "#/";
  });
}

async function recordForm(sec, existing, onSaved) {
  const needs = (t) => sec.fields.some((f) => f.type === t);
  const [contacts, pictures, codesList] = await Promise.all([
    needs("contact") ? api("/api/contacts?limit=500") : { items: [] }, needs("picture") ? api("/api/pictures?limit=500") : { items: [] },
    needs("code") ? api("/api/codes?limit=500") : { items: [] }]);
  const d = existing?.data || {};
  const inputs = sec.fields.filter((f) => f.type !== "formula");
  const computed = sec.fields.filter((f) => f.type === "formula");
  const F = inputs.map((f) => {
    const base = { name: f.key, label: f.label, required: f.required };
    switch (f.type) {
      case "longtext": return { ...base, type: "textarea", full: true };
      case "number": return { ...base, type: "number", step: "any" };
      case "money": return { ...base, placeholder: "0.00" };
      case "date": return { ...base, type: "date" };
      case "boolean": return { ...base, type: "checkbox" };
      case "url": return { ...base, placeholder: "https://" };
      case "email": return { ...base, type: "email" };
      case "select": return { ...base, type: "select", options: [["", "—"], ...f.options.map((o) => [o, o])] };
      case "contact": return { ...base, type: "select", options: [["", "—"], ...contacts.items.map((c) => [c.id, c.full_name])] };
      case "picture": return { ...base, type: "select", options: [["", "—"], ...pictures.items.map((p) => [p.id, p.title || p.original_name])] };
      case "code": return { ...base, type: "select", options: [["", "—"], ...codesList.items.map((c) => [c.id, `${c.label || c.payload} (${c.kind_label})`])] };
      default: return base;
    }
  });
  const values = { ...d };
  inputs.filter((f) => f.type === "money").forEach((f) => { if (d[f.key] != null) values[f.key] = (d[f.key] / 100).toFixed(2); });
  const fxBlock = computed.length ? `<div class="computed"><h3>ƒ Calculated</h3><dl class="kv small">${computed.map((f) =>
    `<dt>${esc(f.label)}</dt><dd>${existing ? renderCell(f, d[f.key]) : `<span class="muted">calculated on save</span>`}
      <span class="muted mono" style="font-size:11px"> = ${esc(f.options.expr)}</span></dd>`).join("")}</dl></div>` : "";
  const m = modal({
    title: existing ? `Edit ${sec.name} record` : `New ${sec.name} record`, wide: F.length > 5, body: formHtml(F, values) + fxBlock,
    foot: `${existing ? `<button class="ghost danger" data-del>Delete</button><button class="ghost" data-hist>History</button><button class="ghost" data-folders>Folders…</button><span class="spacer"></span>` : ""}<button class="ghost" data-close>Cancel</button><button data-save>Save</button>`,
  });
  m.$("[data-hist]")?.addEventListener("click", () => historyModal("section_records", existing.id, sec.name).catch(fail));
  m.$("[data-folders]")?.addEventListener("click", () => editItemFolders("record", existing.id).catch(fail));
  m.$("[data-save]").onclick = async () => {
    try {
      const body = readForm(m.el, F);
      if (existing) await api(`/api/records/${existing.id}`, { method: "PATCH", body });
      else await api(`/api/sections/${sec.id}/records`, { method: "POST", body });
      m.close(); toast("Saved"); onSaved();
    } catch (e) { showErrors(m.el, e); }
  };
  m.$("[data-del]")?.addEventListener("click", async () => {
    if (!(await confirmBox("Delete this record?"))) return;
    await api(`/api/records/${existing.id}`, { method: "DELETE" }).catch(fail); m.close(); onSaved();
  });
}

function renderCell(f, v) {
  if (v == null || v === "") return `<span class="muted">—</span>`;
  const t = resultType(f);
  switch (t) {
    case "money": return `<span class="num">${money(v)}</span>`;
    case "number": return `<span class="num">${esc(typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 6 }) : v)}</span>`;
    case "boolean": return v ? "✓" : `<span class="muted">✗</span>`;
    case "date": return esc(fmtDate(v));
    case "url": return `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v.replace(/^https?:\/\//, "").slice(0, 40))}</a>`;
    case "email": return `<a href="mailto:${esc(v)}">${esc(v)}</a>`;
    case "picture": return `<a href="#/pictures?open=${+v}"><img class="thumb" src="/media/${+v}/thumb"></a>`;
    case "contact": return `<a href="#/contacts/${+v}">contact #${+v}</a>`;
    case "code": return `<img src="/api/codes/${+v}/image.png" style="height:34px;background:#fff;border-radius:4px">`;
    case "select": return `<span class="pill">${esc(v)}</span>`;
    case "longtext": return `<span title="${esc(v)}">${esc(String(v).slice(0, 80))}${String(v).length > 80 ? "…" : ""}</span>`;
    default: return esc(v);
  }
}

route("/s/([a-z0-9-]+)", async (el, slug, params) => {
  const state = { q: "", sort: "", desc: false, filters: {} };
  let sec = await api(`/api/sections/${slug}`);
  const filterable = sec.fields.filter((f) => ["select", "boolean"].includes(f.type));
  const hasFx = sec.fields.some((f) => f.type === "formula");
  el.innerHTML = `
    <div class="page-head"><div><h1>${esc(sec.icon)} ${esc(sec.name)}</h1><p>${esc(sec.description || "Custom section")}</p></div>
      <div class="actions">${hasFx ? `<button class="ghost" id="recalc" title="Recompute every formula">ƒ Recalculate</button>` : ""}
        <button class="ghost" id="edit">Edit fields</button><a class="btn ghost" href="#/data?import=section:${esc(sec.slug)}">Import CSV</a>
        <a class="btn ghost" href="/api/export/section:${esc(sec.slug)}.csv">Export CSV</a>
        <button id="new">＋ New record</button></div></div>
    <div class="toolbar"><input type="search" id="q" placeholder="Search records…">
      ${filterable.map((f) => `<select data-filter="${esc(f.key)}"><option value="">${esc(f.label)}: any</option>
        ${(f.type === "boolean" ? [["true", "Yes"], ["false", "No"]] : f.options.map((o) => [o, o])).map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("")}</select>`).join("")}
      <span class="muted small" id="count"></span></div>
    <div class="card flush"><div class="table-wrap"><table><thead><tr>${sec.fields.map((f) =>
      `<th class="sortable ${isNumeric(f) ? "right" : ""}" data-sort="${esc(f.key)}" ${f.type === "formula" ? `title="= ${esc(f.options.expr)}"` : ""}>${f.type === "formula" ? "ƒ " : ""}${esc(f.label)}</th>`).join("")}
      <th class="sortable" data-sort="updated_at">Updated</th></tr></thead><tbody id="rows"></tbody><tfoot id="totals"></tfoot></table></div></div>
    <p class="muted small" style="margin-top:12px">Stored as JSON documents in <code>section_records</code> — query them in the SQL console with
      <code>json_extract(data, '$.${esc(sec.fields[0]?.key || "field")}')</code>. Formula results are stored too, so they can be sorted and queried.</p>`;
  const load = async () => {
    const f = Object.fromEntries(Object.entries(state.filters).map(([k, v]) => [`f.${k}`, v]));
    const r = await api(`/api/sections/${sec.id}/records?${qs({ q: state.q, sort: state.sort, desc: state.desc ? 1 : "", ...f, limit: 1000 })}`);
    $("#count", el).textContent = `${r.total} record${r.total === 1 ? "" : "s"}${r.total > r.items.length ? ` (showing ${r.items.length})` : ""}`;
    $("#rows", el).innerHTML = r.items.map((rec) => `<tr class="clickable" data-id="${rec.id}">${sec.fields.map((fl) =>
      `<td class="${isNumeric(fl) ? "num" : ""}">${renderCell(fl, rec.data[fl.key])}</td>`).join("")}
      <td class="muted small nowrap">${ago(rec.updated_at)}</td></tr>`).join("")
      || `<tr><td colspan="${sec.fields.length + 1}"><div class="empty"><b>No records</b>Click “New record” to add one.</div></td></tr>`;
    const t = r.totals || {};
    $("#totals", el).innerHTML = Object.keys(t).length ? `<tr class="totals">${sec.fields.map((fl, i) =>
      `<td class="${isNumeric(fl) ? "num" : ""}">${fl.key in t ? `<b>${resultType(fl) === "money" ? money(t[fl.key]) : esc((t[fl.key] ?? 0).toLocaleString(undefined, { maximumFractionDigits: 6 }))}</b>`
        : i === 0 ? `<span class="muted">Σ Total</span>` : ""}</td>`).join("")}<td></td></tr>` : "";
    $$("th.sortable", el).forEach((th) => th.textContent = th.textContent.replace(/ [▲▼]$/, "") + (state.sort === th.dataset.sort ? (state.desc ? " ▼" : " ▲") : ""));
  };
  $("#q", el).addEventListener("input", debounce((e) => { state.q = e.target.value; load().catch(fail); }));
  $$("[data-filter]", el).forEach((s) => s.addEventListener("change", () => {
    if (s.value) state.filters[s.dataset.filter] = s.value; else delete state.filters[s.dataset.filter]; load().catch(fail);
  }));
  $$("th.sortable", el).forEach((th) => th.addEventListener("click", () => {
    if (state.sort === th.dataset.sort) state.desc = !state.desc; else { state.sort = th.dataset.sort; state.desc = false; }
    load().catch(fail);
  }));
  $("#rows", el).addEventListener("click", async (e) => {
    if (e.target.closest("a")) return;
    const tr = e.target.closest("tr[data-id]");
    if (tr) recordForm(sec, await api(`/api/records/${tr.dataset.id}`), () => { load(); loadSectionNav(); }).catch(fail);
  });
  $("#new", el).onclick = () => recordForm(sec, null, () => { load(); loadSectionNav(); }).catch(fail);
  $("#edit", el).onclick = async () => { sec = await api(`/api/sections/${sec.id}`); sectionBuilder(sec).catch(fail); };
  if (params.open) api(`/api/records/${params.open}`).then((rec) => recordForm(sec, rec, () => { load(); loadSectionNav(); })).catch(fail);
  $("#recalc", el)?.addEventListener("click", async () => {
    const r = await api(`/api/sections/${sec.id}/recalculate`, { method: "POST" }).catch(fail);
    if (r) { toast(`Recalculated · ${r.changed} record${r.changed === 1 ? "" : "s"} changed`); load(); }
  });
  await load();
});

// ================================================================ folders
const ITEM_KINDS = {
  note: { label: "Notes", one: "Note", icon: "✎" }, file: { label: "Files", one: "File", icon: "📄" },
  contact: { label: "Contacts", one: "Contact", icon: "☺" }, picture: { label: "Pictures", one: "Picture", icon: "▣" },
  expense: { label: "Expenses", one: "Expense", icon: "$" }, code: { label: "Codes", one: "Code", icon: "▦" },
  record: { label: "Records", one: "Record", icon: "📁" },
};
const PROP_TYPES = { text: "Text", longtext: "Long text", number: "Number", money: "Money", date: "Date", boolean: "Yes / No",
  url: "Link", email: "Email", phone: "Phone" };
const FILE_ICONS = [[/pdf/, "📕"], [/spreadsheet|excel|csv/, "📊"], [/word|document|rtf/, "📝"], [/presentation|powerpoint/, "📽"],
  [/zip|tar|compressed|7z|rar/, "🗜"], [/^audio/, "🎵"], [/^video/, "🎬"], [/^image/, "🖼"], [/^text/, "📃"]];
const fileIcon = (mime) => (FILE_ICONS.find(([re]) => re.test(mime || "")) || [0, "📄"])[1];

let FOLDERS = [];
async function loadFolders() {
  FOLDERS = await api("/api/folders");
  $("#folder-nav").innerHTML = FOLDERS.filter((f) => f.parent_id == null).map((f) => `<a href="#/folders/${f.id}" data-folder-nav="${f.id}">
    <i style="color:${esc(f.color)}">${esc(f.icon)}</i>${esc(f.name)}<span class="muted small" style="margin-left:auto">${f.total_items || ""}</span></a>`).join("")
    || `<div class="muted small" style="padding:4px 10px">No folders yet</div>`;
  markFolderNav();
  return FOLDERS;
}
function markFolderNav() {
  const m = location.hash.match(/^#\/folders\/(\d+)/);
  const top = m ? folderPath(+m[1])[0]?.id : null;
  $$("#folder-nav a").forEach((a) => a.classList.toggle("active", +a.dataset.folderNav === top));
}
// ancestors (root first) from the cached flat list
function folderPath(id) {
  const byId = Object.fromEntries(FOLDERS.map((f) => [f.id, f]));
  const out = [];
  for (let f = byId[id], guard = 0; f && guard < 50; f = byId[f.parent_id], guard++) out.unshift(f);
  return out;
}
// depth-first list for tree pickers: [{folder, depth}]
function folderTree(excludeId = null) {
  const kids = {};
  FOLDERS.forEach((f) => (kids[f.parent_id ?? "root"] ||= []).push(f));
  const out = [];
  const walk = (pid, depth) => (kids[pid] || []).forEach((f) => {
    if (f.id === excludeId) return; // skip a folder and its whole subtree (can't move into itself)
    out.push({ folder: f, depth }); walk(f.id, depth + 1);
  });
  walk("root", 0);
  return out;
}

// ---- reusable: choose folders (checkbox tree)
function folderPicker({ title = "Folders", selected = [], single = false, exclude = null, allowRoot = false } = {}) {
  return new Promise(async (resolve) => {
    await loadFolders();
    const sel = new Set(selected.map(Number));
    const tree = folderTree(exclude);
    const m = modal({
      title, body: tree.length || allowRoot ? `<div class="folder-pick">
        ${allowRoot ? `<label class="check"><input type="radio" name="fp" value="" ${sel.size ? "" : "checked"}> <i>🏠</i> Top level</label>` : ""}
        ${tree.map(({ folder: f, depth }) => `<label class="check" style="padding-left:${depth * 20 + 4}px">
          <input type="${single ? "radio" : "checkbox"}" name="fp" value="${f.id}" ${sel.has(f.id) ? "checked" : ""}>
          <i style="color:${esc(f.color)}">${esc(f.icon)}</i> ${esc(f.name)}</label>`).join("")}</div>`
        : `<div class="empty"><b>No folders yet</b>Create one first.</div>`,
      foot: `<button class="ghost" id="fp-new">＋ New folder</button><span class="spacer"></span><button class="ghost" data-close>Cancel</button><button data-ok>Done</button>`,
    });
    let done = false;
    m.$("[data-ok]").onclick = () => {
      done = true;
      const ids = m.$$("input[name=fp]:checked").map((i) => i.value).filter(Boolean).map(Number);
      m.close(); resolve(single ? (ids[0] ?? null) : ids);
    };
    m.$("#fp-new").onclick = () => { m.close(); folderForm(null, null, () => folderPicker({ title, selected, single, exclude, allowRoot }).then(resolve)); done = true; };
    m.el.addEventListener("click", (e) => { if (e.target.closest("[data-close]") && !done) resolve(undefined); });
  });
}

// "Folders…" button on any item's own page
async function editItemFolders(type, id) {
  const current = await api(`/api/items/${type}/${id}/folders`);
  const ids = await folderPicker({ title: `Put this ${ITEM_KINDS[type].one.toLowerCase()} in folders`, selected: current.map((f) => f.id) });
  if (ids === undefined) return null;
  const r = await api(`/api/items/${type}/${id}/folders`, { method: "PUT", body: { folder_ids: ids } });
  toast(r.length ? `In ${r.map((f) => f.name).join(", ")}` : "Not in any folder");
  loadFolders();
  return r;
}

// ---- folder create/edit (name, look, parent, field template)
function templateRows(rows) {
  return rows.map((t, i) => `<div class="tpl-row" data-i="${i}">
    <input data-k="name" value="${esc(t.name)}" placeholder="Field name, e.g. VIN">
    <select data-k="type">${Object.entries(PROP_TYPES).map(([k, l]) => `<option value="${k}" ${t.type === k ? "selected" : ""}>${l}</option>`).join("")}</select>
    <button class="icon-btn" data-up title="Move up">↑</button><button class="icon-btn" data-rm title="Remove">✕</button></div>`).join("");
}

async function folderForm(existing, parentId, onSaved) {
  await loadFolders();
  const tpl = (existing?.template || []).map((t) => ({ ...t }));
  let parent = existing ? existing.parent_id : parentId;
  const parentLabel = () => parent ? folderPath(parent).map((f) => `${f.icon} ${f.name}`).join(" › ") : "🏠 Top level";
  const m = modal({
    title: existing ? `Edit “${existing.name}”` : "New folder", wide: true,
    body: `<div class="form-grid" style="grid-template-columns:80px 1fr 90px">
        <label class="field"><span>Icon</span><input id="f-icon" value="${esc(existing?.icon || "📁")}" maxlength="4" style="text-align:center;font-size:18px"></label>
        <label class="field"><span class="req">Name</span><input id="f-name" value="${esc(existing?.name || "")}" placeholder="e.g. Car, Taxes 2026, Recipes, Pets"></label>
        <label class="field"><span>Color</span><input id="f-color" type="color" value="${esc(existing?.color || "#6366f1")}"></label>
        <label class="field" style="grid-column:1/-1"><span>Description</span><input id="f-desc" value="${esc(existing?.description || "")}"></label></div>
      <div class="list-row" style="margin-top:8px"><span class="muted small" style="flex:1">Inside: <b id="f-parent">${esc(parentLabel())}</b></span>
        <button class="ghost small" id="f-move">Change…</button></div>
      <h3 style="margin-top:16px">Field template</h3>
      <p class="muted small" style="margin-top:0">New notes created in this folder start with these fields. Each note can still add,
        rename or remove its own. For example, a <i>Vehicles</i> folder could use Make, Model, VIN and Insurance due.</p>
      <div id="tpl"></div><button class="ghost small" id="tpl-add" style="margin-top:8px">＋ Add field</button>`,
    foot: `${existing ? `<button class="ghost danger" data-del>Delete folder</button><button class="ghost" data-hist>History</button><span class="spacer"></span>` : ""}
      <button class="ghost" data-close>Cancel</button><button data-save>${existing ? "Save" : "Create folder"}</button>`,
  });
  const render = () => { m.$("#tpl").innerHTML = templateRows(tpl) || `<div class="muted small">No template fields.</div>`; };
  render();
  m.$("#tpl").addEventListener("input", (e) => { const r = e.target.closest("[data-i]"); if (r && e.target.dataset.k) tpl[r.dataset.i][e.target.dataset.k] = e.target.value; });
  m.$("#tpl").addEventListener("click", (e) => {
    const r = e.target.closest("[data-i]"); if (!r) return; const i = +r.dataset.i;
    if (e.target.closest("[data-rm]")) { tpl.splice(i, 1); render(); }
    if (e.target.closest("[data-up]") && i > 0) { [tpl[i - 1], tpl[i]] = [tpl[i], tpl[i - 1]]; render(); }
  });
  m.$("#tpl-add").onclick = () => { tpl.push({ name: "", type: "text" }); render(); $$("#tpl [data-k=name]", m.el).at(-1).focus(); };
  m.$("#f-move").onclick = async () => {
    const r = await folderPicker({ title: "Put this folder inside…", selected: parent ? [parent] : [], single: true, exclude: existing?.id, allowRoot: true });
    if (r !== undefined) { parent = r; m.$("#f-parent").textContent = parentLabel(); }
  };
  m.$("[data-save]").onclick = async () => {
    const b = { name: m.$("#f-name").value, icon: m.$("#f-icon").value, color: m.$("#f-color").value, description: m.$("#f-desc").value,
      parent_id: parent, template: tpl.filter((t) => t.name.trim()) };
    try {
      const f = existing ? await api(`/api/folders/${existing.id}`, { method: "PATCH", body: b }) : await api("/api/folders", { method: "POST", body: b });
      m.close(); toast(existing ? "Folder saved" : "Folder created"); await loadFolders(); onSaved?.(f);
    } catch (e) { fail(e); }
  };
  m.$("[data-hist]")?.addEventListener("click", () => historyModal("folders", existing.id, existing.name).catch(fail));
  m.$("[data-del]")?.addEventListener("click", async () => {
    const subs = FOLDERS.filter((f) => folderPath(f.id).some((a) => a.id === existing.id)).length - 1;
    if (!(await confirmBox(`Delete “${existing.name}”${subs ? ` and its ${subs} subfolder${subs === 1 ? "" : "s"}` : ""}? The items inside are NOT deleted — they're only removed from the folder.`))) return;
    await api(`/api/folders/${existing.id}`, { method: "DELETE" }).catch(fail);
    m.close(); toast("Folder deleted"); await loadFolders();
    location.hash = existing.parent_id ? `#/folders/${existing.parent_id}` : "#/folders";
  });
}

// ---- notes: title, body, and freely editable typed fields
function propInput(p, i) {
  const v = p.value;
  switch (p.type) {
    case "longtext": return `<textarea data-v="${i}" rows="2">${esc(v ?? "")}</textarea>`;
    case "number": return `<input data-v="${i}" type="number" step="any" value="${esc(v ?? "")}">`;
    case "money": return `<input data-v="${i}" value="${esc(v ?? "")}" placeholder="0.00">`;
    case "date": return `<input data-v="${i}" type="date" value="${esc(v ?? "")}">`;
    case "boolean": return `<label class="check"><input data-v="${i}" type="checkbox" ${v ? "checked" : ""}> yes</label>`;
    case "url": return `<input data-v="${i}" value="${esc(v ?? "")}" placeholder="https://">`;
    case "email": return `<input data-v="${i}" type="email" value="${esc(v ?? "")}">`;
    case "phone": return `<input data-v="${i}" type="tel" value="${esc(v ?? "")}">`;
    default: return `<input data-v="${i}" value="${esc(v ?? "")}">`;
  }
}
function propDisplay(p) {
  const v = p.value;
  if (v == null || v === "") return `<span class="muted">—</span>`;
  if (p.type === "money") return money(v);
  if (p.type === "boolean") return v ? "✓ yes" : "✗ no";
  if (p.type === "date") return esc(fmtDate(v));
  if (p.type === "url") return `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>`;
  if (p.type === "email") return `<a href="mailto:${esc(v)}">${esc(v)}</a>`;
  if (p.type === "phone") return `<a href="tel:${esc(v)}">${esc(v)}</a>`;
  return esc(v);
}

async function noteEditor(noteId, { folderId = null, onSaved } = {}) {
  let note = noteId ? await api(`/api/notes/${noteId}`) : null;
  // money is edited as a dollar string ("12.50"); the server converts it to cents
  const forEdit = (p) => (p.type === "money" && p.value != null ? { ...p, value: (p.value / 100).toFixed(2) } : { ...p });
  let props = note ? note.properties.map(forEdit)
    : (folderId ? (FOLDERS.find((f) => f.id === folderId)?.template || []).map((t) => ({ ...t, value: null })) : []);
  let folderIds = note ? note.folders.map((f) => f.id) : (folderId ? [folderId] : []);
  const m = modal({
    title: note ? "Edit note" : "New note", wide: true,
    body: `<div style="display:flex;gap:10px;align-items:center"><input id="n-title" value="${esc(note?.title || "")}" placeholder="Title"
        style="flex:1;font-size:17px;font-weight:600"><label class="check small"><input type="checkbox" id="n-pin" ${note?.pinned ? "checked" : ""}> 📌 Pin</label></div>
      <h3 style="margin-top:16px">Fields</h3>
      <div id="props" class="props"></div>
      <div class="actions" style="margin-top:8px"><button class="ghost small" id="p-add">＋ Add field</button>
        <span class="muted small">Name it anything (Serial #, Due date, Rating…) and pick its type.</span></div>
      <h3 style="margin-top:16px">Notes</h3>
      <textarea id="n-body" rows="8" placeholder="Anything you want to remember…" style="width:100%">${esc(note?.body || "")}</textarea>
      <div class="list-row" style="margin-top:10px"><span class="muted small">Folders:</span><span id="n-folders" style="flex:1" class="actions"></span>
        <button class="ghost small" id="n-fold">Change…</button></div>`,
    foot: `${note ? `<button class="ghost danger" data-del>Delete</button><button class="ghost" data-hist>History</button><span class="spacer"></span>` : ""}
      <button class="ghost" data-close>Cancel</button><button data-save>${note ? "Save" : "Create note"}</button>`,
  });
  const renderFolders = () => {
    m.$("#n-folders").innerHTML = folderIds.map((id) => { const f = FOLDERS.find((x) => x.id === id);
      return f ? `<span class="pill" style="border-color:${esc(f.color)}">${esc(f.icon)} ${esc(f.name)}</span>` : ""; }).join("") || `<span class="muted small">none</span>`;
  };
  const render = () => {
    m.$("#props").innerHTML = props.map((p, i) => `<div class="prop-row" data-i="${i}">
      <input class="prop-name" data-k="name" value="${esc(p.name)}" placeholder="Field name">
      <select data-k="type">${Object.entries(PROP_TYPES).map(([k, l]) => `<option value="${k}" ${p.type === k ? "selected" : ""}>${l}</option>`).join("")}</select>
      <div class="prop-val">${propInput(p, i)}</div>
      <span class="prop-tools"><button class="icon-btn" data-up title="Move up">↑</button><button class="icon-btn" data-rm title="Remove field">✕</button></span>
      <span class="field-error" data-err-p="${i}"></span></div>`).join("") || `<div class="muted small">No fields yet. Add one below.</div>`;
  };
  const readValues = () => m.$$("[data-v]").forEach((el) => {
    const p = props[+el.dataset.v];
    p.value = el.type === "checkbox" ? el.checked : el.value;
  });
  render(); renderFolders();
  m.$("#props").addEventListener("input", (e) => {
    const r = e.target.closest("[data-i]"); const k = e.target.dataset.k; if (!r || !k) return;
    readValues();
    const p = props[+r.dataset.i];
    if (k === "type") {
      const wasBool = p.type === "boolean";
      p.type = e.target.value;
      if (p.type === "boolean") p.value = !!p.value && p.value !== "false";
      else if (wasBool) p.value = "";
      render();
    } else p[k] = e.target.value;
  });
  m.$("#props").addEventListener("click", (e) => {
    const r = e.target.closest("[data-i]"); if (!r) return; const i = +r.dataset.i; readValues();
    if (e.target.closest("[data-rm]")) { props.splice(i, 1); render(); }
    if (e.target.closest("[data-up]") && i > 0) { [props[i - 1], props[i]] = [props[i], props[i - 1]]; render(); }
  });
  m.$("#p-add").onclick = () => { readValues(); props.push({ name: "", type: "text", value: null }); render(); m.$$(".prop-name").at(-1).focus(); };
  m.$("#n-fold").onclick = async () => {
    const r = await folderPicker({ title: "Folders for this note", selected: folderIds });
    if (r !== undefined) { folderIds = r; renderFolders(); }
  };
  m.$("[data-save]").onclick = async () => {
    readValues();
    const outProps = props.filter((p) => p.name.trim()).map((p) => ({ name: p.name, type: p.type, value: p.value }));
    const b = { title: m.$("#n-title").value, body: m.$("#n-body").value, pinned: m.$("#n-pin").checked, properties: outProps };
    try {
      if (note) note = await api(`/api/notes/${note.id}`, { method: "PATCH", body: { ...b, folder_ids: folderIds } });
      else {
        note = await api("/api/notes", { method: "POST", body: { ...b, folder_id: folderIds[0] ?? null } });
        if (folderIds.length > 1) await api(`/api/items/note/${note.id}/folders`, { method: "PUT", body: { folder_ids: folderIds } });
      }
      m.close(); toast("Note saved"); loadFolders(); onSaved?.(note);
    } catch (e) {
      const msg = e.fields?.properties || e.fields?.title || e.message;
      toast(msg, "error");
    }
  };
  m.$("[data-hist]")?.addEventListener("click", () => historyModal("notes", note.id, note.title).catch(fail));
  m.$("[data-del]")?.addEventListener("click", async () => {
    if (!(await confirmBox(`Delete “${note.title}”? You can restore it from Data › Recently deleted.`))) return;
    await api(`/api/notes/${note.id}`, { method: "DELETE" }).catch(fail);
    m.close(); toast("Note deleted"); loadFolders(); onSaved?.(null);
  });
  setTimeout(() => m.$("#n-title").focus(), 40);
}

// ---- item picker: add existing things of any type to a folder
function itemPicker(folderId, onDone) {
  let type = "contact";
  const chosen = new Map();
  const m = modal({
    title: "Add existing items", wide: true,
    body: `<div class="seg" id="ip-types" style="flex-wrap:wrap">${Object.entries(ITEM_KINDS).map(([k, v]) =>
      `<button data-t="${k}" class="${k === type ? "on" : ""}">${v.icon} ${v.label}</button>`).join("")}</div>
      <input type="search" id="ip-q" placeholder="Search…" style="width:100%;margin:12px 0">
      <div id="ip-list" class="pick-list"></div>`,
    foot: `<span class="muted small" id="ip-count">0 selected</span><span class="spacer"></span><button class="ghost" data-close>Cancel</button><button data-ok>Add to folder</button>`,
  });
  const load = async () => {
    const items = await api(`/api/items/lookup?${qs({ type, q: m.$("#ip-q").value, limit: 50 })}`);
    m.$("#ip-list").innerHTML = items.map((it) => `<label class="pick-item"><input type="checkbox" data-key="${it.item_type}:${it.id}" ${chosen.has(`${it.item_type}:${it.id}`) ? "checked" : ""}>
      ${it.thumb ? `<img src="${esc(it.thumb)}" alt="">` : `<span class="pick-ico">${ITEM_KINDS[it.item_type].icon}</span>`}
      <span><b>${esc(it.title)}</b><div class="muted small">${esc(it.subtitle || "")}</div></span></label>`).join("")
      || `<div class="empty">Nothing found</div>`;
  };
  m.$("#ip-types").addEventListener("click", (e) => { const b = e.target.closest("[data-t]"); if (!b) return;
    type = b.dataset.t; m.$$("#ip-types button").forEach((x) => x.classList.toggle("on", x === b)); load(); });
  m.$("#ip-q").addEventListener("input", debounce(load, 200));
  m.$("#ip-list").addEventListener("change", (e) => {
    const k = e.target.dataset.key; if (!k) return;
    if (e.target.checked) chosen.set(k, true); else chosen.delete(k);
    m.$("#ip-count").textContent = `${chosen.size} selected`;
  });
  m.$("[data-ok]").onclick = async () => {
    if (!chosen.size) return m.close();
    const items = [...chosen.keys()].map((k) => { const [t, id] = k.split(":"); return { item_type: t, item_id: +id }; });
    try { const r = await api(`/api/folders/${folderId}/items`, { method: "POST", body: { items } });
      m.close(); toast(`Added ${r.added} item${r.added === 1 ? "" : "s"}`); onDone(); } catch (e) { fail(e); }
  };
  load();
}

async function uploadFiles(fileList, folderId) {
  const fd = new FormData();
  [...fileList].forEach((f) => fd.append("file", f));
  if (folderId) fd.append("folder_id", folderId);
  // images go to Pictures (thumbnails, EXIF); everything else is a File
  const imgs = [...fileList].filter((f) => /^image\/(jpeg|png|gif|webp|bmp|tiff|heic|heif)$/.test(f.type) || /\.(heic|heif)$/i.test(f.name));
  const docs = [...fileList].filter((f) => !imgs.includes(f));
  let n = 0;
  if (docs.length) {
    const d = new FormData(); docs.forEach((f) => d.append("file", f)); if (folderId) d.append("folder_id", folderId);
    const r = await api("/api/files", { method: "POST", form: d }); n += r.items.length;
    r.errors.forEach((e) => toast(`${e.name}: ${Object.values(e.errors).join("; ")}`, "error"));
  }
  if (imgs.length) {
    const r = await uploadPictures(imgs);
    if (folderId && r.items.length) await api(`/api/folders/${folderId}/items`, { method: "POST",
      body: { items: r.items.map((p) => ({ item_type: "picture", item_id: p.id })) } });
    n += r.items.length;
  }
  if (docs.length) toast(`Uploaded ${n} item${n === 1 ? "" : "s"}`);
  return n;
}

function itemCard(it, folderId) {
  const k = ITEM_KINDS[it.item_type];
  const vis = it.thumb ? `<img src="${esc(it.thumb)}" alt="" loading="lazy">`
    : `<span class="card-ico">${it.item_type === "file" ? fileIcon(it.mime) : k.icon}</span>`;
  return `<div class="item-card" draggable="true" data-type="${it.item_type}" data-id="${it.id}" data-href="${esc(it.href)}">
    <div class="item-vis ${it.item_type === "code" ? "code" : ""}">${vis}</div>
    <div class="item-meta"><div class="item-title">${it.pinned ? "📌 " : ""}${esc(it.title)}</div>
      <div class="muted small item-sub">${esc(it.subtitle || "")}</div></div>
    <div class="item-foot"><span class="pill">${k.icon} ${k.one}</span>
      ${folderId ? `<span><button class="icon-btn" data-act="move" title="Move to another folder">⇄</button>
      <button class="icon-btn" data-act="remove" title="Remove from this folder">✕</button></span>` : ""}</div></div>`;
}

function openItem(card) {
  const { type, id, href } = card.dataset;
  if (type === "note") return noteEditor(+id, { onSaved: () => navigate() });
  if (type === "file") return window.open(`/files/${id}/view`, "_blank", "noopener");
  location.hash = href.slice(1);
}

route("/folders", async (el) => {
  await loadFolders();
  const top = FOLDERS.filter((f) => f.parent_id == null);
  const unfiled = await api("/api/notes?unfiled=1&limit=1");
  el.innerHTML = `
    <div class="page-head"><div><h1>Folders</h1><p>Group anything together: notes with your own fields, files, contacts, pictures, expenses, codes and records.</p></div>
      <div class="actions"><button class="ghost" id="new-note">＋ Note</button><button id="new">＋ New folder</button></div></div>
    <div class="folder-grid">${top.map(folderTile).join("")}
      <a class="folder-tile ghosted" href="#/notes?unfiled=1"><span class="ft-icon">🗂</span><b>Unfiled notes</b><span class="muted small">${unfiled.total}</span></a></div>
    ${top.length ? "" : `<div class="card empty" style="margin-top:16px"><b>No folders yet</b>Create one, e.g. “Car”, “Taxes 2026”, “Recipes”, “Pets” — then fill it with any mix of information.</div>`}`;
  $("#new", el).onclick = () => folderForm(null, null, (f) => (location.hash = `#/folders/${f.id}`));
  $("#new-note", el).onclick = () => noteEditor(null, { onSaved: () => navigate() });
});

function folderTile(f) {
  return `<a class="folder-tile" href="#/folders/${f.id}" data-drop-folder="${f.id}" style="--fc:${esc(f.color)}">
    <span class="ft-icon">${esc(f.icon)}</span><b>${esc(f.name)}</b>
    <span class="muted small">${f.total_items} item${f.total_items === 1 ? "" : "s"}${f.folder_count ? ` · ${f.folder_count} folder${f.folder_count === 1 ? "" : "s"}` : ""}</span></a>`;
}

route("/folders/(\\d+)", async (el, id) => {
  const fid = +id;
  const state = { type: "", q: "" };
  await loadFolders();
  let f = await api(`/api/folders/${fid}`);
  const shell = () => {
    el.innerHTML = `
      <div class="crumbs"><a href="#/folders">Folders</a>${f.path.map((p, i) => i === f.path.length - 1 ? ` › <b>${esc(p.icon)} ${esc(p.name)}</b>`
        : ` › <a href="#/folders/${p.id}" data-drop-folder="${p.id}">${esc(p.icon)} ${esc(p.name)}</a>`).join("")}</div>
      <div class="page-head folder-head" style="--fc:${esc(f.color)}"><div><h1><span class="fh-icon">${esc(f.icon)}</span> ${esc(f.name)}</h1>
        <p>${esc(f.description || "")}${f.template.length ? ` <span class="pill" title="New notes start with these fields">template: ${esc(f.template.map((t) => t.name).join(", "))}</span>` : ""}</p></div>
        <div class="actions"><button class="ghost" id="edit">Edit folder</button><button class="ghost" id="add">＋ Existing…</button>
          <label class="btn ghost">⤒ Upload<input type="file" multiple hidden id="up"></label>
          <button class="ghost" id="sub">＋ Subfolder</button><button id="note">＋ Note</button></div></div>
      <div class="toolbar"><input type="search" id="q" placeholder="Search this folder…" value="${esc(state.q)}">
        <div class="seg" id="types"><button data-t="" class="${state.type ? "" : "on"}">All</button>${Object.entries(ITEM_KINDS)
          .filter(([k]) => f.type_counts[k]).map(([k, v]) => `<button data-t="${k}" class="${state.type === k ? "on" : ""}">${v.icon} ${v.label} ${f.type_counts[k]}</button>`).join("")}</div></div>
      ${f.subfolders.length ? `<div class="folder-grid" style="margin-bottom:18px">${f.subfolders.map(folderTile).join("")}</div>` : ""}
      <div class="item-grid" id="items">${f.items.map((it) => itemCard(it, fid)).join("")}</div>
      ${f.items.length ? "" : `<div class="card empty drop-hint"><b>${state.q || state.type ? "Nothing matches" : "This folder is empty"}</b>
        Add a note, drop files anywhere on this page, or use “＋ Existing…” to add contacts, pictures, expenses, codes or records.</div>`}`;
    bind();
  };
  const reload = async () => { f = await api(`/api/folders/${fid}?${qs({ type: state.type, q: state.q })}`); await loadFolders(); shell(); };
  const bind = () => {
    $("#edit", el).onclick = () => folderForm(f, null, () => reload());
    $("#sub", el).onclick = () => folderForm(null, fid, () => reload());
    $("#note", el).onclick = () => noteEditor(null, { folderId: fid, onSaved: reload });
    $("#add", el).onclick = () => itemPicker(fid, reload);
    $("#up", el).onchange = async (e) => { if (e.target.files.length) { await uploadFiles(e.target.files, fid).catch(fail); reload(); } };
    $("#q", el).addEventListener("input", debounce((e) => { state.q = e.target.value; reload().then(() => { const q = $("#q", el); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }); }, 250));
    $("#types", el).addEventListener("click", (e) => { const b = e.target.closest("[data-t]"); if (b) { state.type = b.dataset.t; reload(); } });
    $("#items", el).addEventListener("click", async (e) => {
      const card = e.target.closest(".item-card"); if (!card) return;
      const act = e.target.closest("[data-act]")?.dataset.act;
      const { type, id: itemId } = card.dataset;
      if (act === "remove") {
        await api(`/api/folders/${fid}/items/${type}/${itemId}`, { method: "DELETE" }).catch(fail);
        toast("Removed from folder (the item itself still exists)"); return reload();
      }
      if (act === "move") {
        const to = await folderPicker({ title: "Move to…", single: true, exclude: null });
        if (to && to !== fid) { await api(`/api/folders/${fid}/items/${type}/${itemId}/move`, { method: "POST", body: { to_folder: to } }).catch(fail); toast("Moved"); reload(); }
        return;
      }
      openItem(card);
    });
  };
  // drag an item onto a subfolder/breadcrumb to move it; drop files from the desktop to upload
  const onDragStart = (e) => { const c = e.target.closest?.(".item-card"); if (c) e.dataTransfer.setData("text/x-dv-item", `${c.dataset.type}:${c.dataset.id}`); };
  const onDragOver = (e) => {
    const isFiles = e.dataTransfer.types.includes("Files"), isItem = e.dataTransfer.types.includes("text/x-dv-item");
    if (!isFiles && !isItem) return;
    e.preventDefault();
    $$(".drop-over", el).forEach((x) => x.classList.remove("drop-over"));
    const t = isItem ? e.target.closest("[data-drop-folder]") : el;
    t?.classList.add("drop-over");
  };
  const onDrop = async (e) => {
    $$(".drop-over", el).forEach((x) => x.classList.remove("drop-over")); el.classList.remove("drop-over");
    if (e.dataTransfer.files.length) { e.preventDefault(); await uploadFiles(e.dataTransfer.files, fid).catch(fail); return reload(); }
    const item = e.dataTransfer.getData("text/x-dv-item"); const target = e.target.closest("[data-drop-folder]");
    if (item && target && +target.dataset.dropFolder !== fid) {
      e.preventDefault();
      const [type, itemId] = item.split(":");
      await api(`/api/folders/${fid}/items/${type}/${itemId}/move`, { method: "POST", body: { to_folder: +target.dataset.dropFolder } }).catch(fail);
      toast("Moved"); reload();
    }
  };
  const onDragLeave = (e) => { if (e.target === el) el.classList.remove("drop-over"); };
  el.addEventListener("dragstart", onDragStart); el.addEventListener("dragover", onDragOver);
  el.addEventListener("drop", onDrop); el.addEventListener("dragleave", onDragLeave);
  shell();
  markFolderNav();
  return () => { el.removeEventListener("dragstart", onDragStart); el.removeEventListener("dragover", onDragOver);
    el.removeEventListener("drop", onDrop); el.removeEventListener("dragleave", onDragLeave); };
});

// ---- all notes & files (including unfiled ones)
route("/notes(?:/(\\d+))?", async (el, id, params) => {
  await loadFolders();
  const state = { q: "", unfiled: params.unfiled === "1", tab: params.tab || "notes" };
  el.innerHTML = `
    <div class="page-head"><div><h1>Notes &amp; Files</h1><p>Everything you've written or attached, whether or not it's in a folder.</p></div>
      <div class="actions"><label class="btn ghost">⤒ Upload files<input type="file" multiple hidden id="up"></label><button id="new">＋ Note</button></div></div>
    <div class="toolbar"><div class="seg" id="tabs"><button data-tab="notes">✎ Notes</button><button data-tab="files">📄 Files</button></div>
      <input type="search" id="q" placeholder="Search…"><label class="check small" id="unf-wrap"><input type="checkbox" id="unf" ${state.unfiled ? "checked" : ""}> Only unfiled</label>
      <span class="muted small" id="count"></span></div>
    <div class="item-grid" id="items"></div>`;
  const load = async () => {
    $$("#tabs button", el).forEach((b) => b.classList.toggle("on", b.dataset.tab === state.tab));
    $("#unf-wrap", el).hidden = state.tab !== "notes";
    let cards;
    if (state.tab === "notes") {
      const r = await api(`/api/notes?${qs({ q: state.q, unfiled: state.unfiled ? 1 : "" })}`);
      $("#count", el).textContent = `${r.total} note${r.total === 1 ? "" : "s"}`;
      cards = r.items.map((n) => ({ item_type: "note", id: n.id, title: n.title, pinned: n.pinned, href: `#/notes/${n.id}`,
        subtitle: [n.folders.map((x) => `${x.icon} ${x.name}`).join(", "), n.properties.filter((p) => p.value != null && p.value !== "").length
          ? `${n.properties.filter((p) => p.value != null && p.value !== "").length} fields` : "", (n.body.split("\n")[0] || "").slice(0, 80)].filter(Boolean).join(" · ") }));
    } else {
      const r = await api(`/api/files?${qs({ q: state.q })}`);
      $("#count", el).textContent = `${r.total} file${r.total === 1 ? "" : "s"}`;
      cards = r.items.map((x) => ({ item_type: "file", id: x.id, title: x.original_name, mime: x.mime, href: `/files/${x.id}/download`,
        subtitle: `${fmtBytes(x.size_bytes)} · ${x.mime}` }));
    }
    $("#items", el).innerHTML = cards.map((c) => itemCard(c, null).replace('<div class="item-foot">',
      `<div class="item-foot">${c.item_type === "file" ? `<span><a class="icon-btn" href="/files/${c.id}/download" title="Download">⤓</a>
        <button class="icon-btn" data-act="folders" title="Folders">📁</button><button class="icon-btn" data-act="del-file" title="Delete file">🗑</button></span>` : ""}`)).join("")
      || `<div class="card empty" style="grid-column:1/-1"><b>Nothing here yet</b></div>`;
  };
  $("#tabs", el).addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) { state.tab = b.dataset.tab; load(); } });
  $("#q", el).addEventListener("input", debounce((e) => { state.q = e.target.value; load(); }, 250));
  $("#unf", el).onchange = (e) => { state.unfiled = e.target.checked; load(); };
  $("#new", el).onclick = () => noteEditor(null, { onSaved: load });
  $("#up", el).onchange = async (e) => { if (e.target.files.length) { await uploadFiles(e.target.files, null).catch(fail); state.tab = "files"; load(); } };
  $("#items", el).addEventListener("click", async (e) => {
    if (e.target.closest("a")) return;
    const card = e.target.closest(".item-card"); if (!card) return;
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "folders") return editItemFolders("file", +card.dataset.id).then(load).catch(fail);
    if (act === "del-file") {
      if (!(await confirmBox("Delete this file permanently? (Files can't be restored from Recently deleted.)"))) return;
      await api(`/api/files/${card.dataset.id}`, { method: "DELETE" }).catch(fail); toast("File deleted"); return load();
    }
    if (card.dataset.type === "note") return noteEditor(+card.dataset.id, { onSaved: load });
    openItem(card);
  });
  await load();
  if (id) noteEditor(+id, { onSaved: () => { history.replaceState(null, "", "#/notes"); load(); } }).catch(fail);
});

// ============================================================ SQL console
route("/sql", async (el, params) => {
  const [schema, examples] = await Promise.all([api("/api/schema"), api("/api/query/examples")]);
  let saved = await api("/api/queries");
  let last = null;
  let stored = null;
  try { stored = localStorage.getItem("dv.sql"); } catch { /* storage blocked */ }
  const initial = params.sql || stored || examples[0].sql;
  el.innerHTML = `
    <div class="page-head"><div><h1>SQL Console</h1><p>Read-only, time-boxed queries against the live database. <kbd>Ctrl</kbd>+<kbd>Enter</kbd> to run.</p></div></div>
    <div class="sql-layout">
      <div class="card schema-tree"><h3>Schema</h3>${schema.map((t) => `<details><summary><span>${t.type === "view" ? "👁 " : ""}${esc(t.name)}</span>
        <span class="muted small">${t.row_count ?? ""}</span></summary><ul>
        ${t.columns.map((c) => `<li data-col="${esc(c.name)}"><span>${c.pk ? "🔑 " : ""}${esc(c.name)}</span><span class="t">${esc(c.type || "")}</span></li>`).join("")}
        ${t.foreign_keys.map((f) => `<li class="muted" data-table="${esc(f.table)}">→ ${esc(f.from)} ⟶ ${esc(f.table)}</li>`).join("")}</ul>
        <div class="actions" style="padding-left:14px"><button class="ghost small" data-peek="${esc(t.name)}">SELECT *</button>
          <button class="ghost small" data-ddl="${esc(t.name)}">DDL</button></div></details>`).join("")}</div>
      <div>
        <div class="card">
          <textarea class="editor" id="sql" spellcheck="false">${esc(initial)}</textarea>
          <div class="actions" style="margin-top:10px"><button id="run">▶ Run</button><button class="ghost" id="explain">Explain plan</button>
            <select id="examples"><option value="">Examples…</option>${examples.map((e, i) => `<option value="${i}">${esc(e.name)}</option>`).join("")}</select>
            <select id="saved"></select><button class="ghost" id="save">Save query</button>
            <span style="flex:1"></span><button class="ghost" id="csv" disabled>Export CSV</button></div>
          <div id="out"></div></div></div></div>`;
  const ed = $("#sql", el);
  const renderSaved = () => { $("#saved", el).innerHTML = `<option value="">Saved queries (${saved.length})…</option>` +
    saved.map((q) => `<option value="${q.id}">${esc(q.name)}</option>`).join(""); };
  renderSaved();
  const run = async (explain = false) => {
    try { localStorage.setItem("dv.sql", ed.value); } catch { /* storage blocked */ }
    const sel = ed.value.substring(ed.selectionStart, ed.selectionEnd).trim();
    const sql = sel || ed.value;
    $("#out", el).innerHTML = `<div class="result-meta">Running…</div>`;
    try {
      const r = await api("/api/query", { method: "POST", body: { sql, explain } });
      last = { sql, r };
      $("#csv", el).disabled = explain;
      $("#out", el).innerHTML = `<div class="result-meta"><b>${r.row_count} row${r.row_count === 1 ? "" : "s"}</b><span>${r.elapsed_ms} ms</span>
          ${r.truncated ? `<span class="pill">truncated to ${r.row_count}</span>` : ""}${sel ? `<span class="pill">ran selection</span>` : ""}</div>
        ${r.columns.length ? `<div class="table-wrap" style="max-height:55vh;border:1px solid var(--border);border-radius:10px"><table class="results"><thead><tr>
          ${r.columns.map((c, i) => `<th class="${r.types[i] === "number" ? "right" : ""}">${esc(c)}</th>`).join("")}</tr></thead><tbody>
          ${r.rows.map((row) => `<tr>${row.map((v, i) => v === null ? `<td class="null">NULL</td>` :
            `<td class="${r.types[i] === "number" ? "num" : ""}" title="${esc(v)}">${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : ""}`;
    } catch (e) { $("#out", el).innerHTML = `<div class="error-box">${esc(e.fields.sql || e.message)}</div>`; }
  };
  $("#run", el).onclick = () => run();
  $("#explain", el).onclick = () => run(true);
  ed.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run(); }
    if (e.key === "Tab") { e.preventDefault(); const s = ed.selectionStart; ed.setRangeText("  ", s, ed.selectionEnd, "end"); }
  });
  $("#examples", el).onchange = (e) => { if (e.target.value !== "") { ed.value = examples[e.target.value].sql; e.target.value = ""; run(); } };
  $("#saved", el).onchange = (e) => { const q = saved.find((x) => x.id === +e.target.value); if (q) { ed.value = q.sql; run(); } e.target.value = ""; };
  $("#save", el).onclick = () => {
    const F = [{ name: "name", label: "Name", required: true, full: true }, { name: "description", label: "Description", full: true }];
    const m = modal({ title: "Save query", body: formHtml(F) + `<pre class="editor" style="min-height:0;margin-top:12px;max-height:200px;overflow:auto">${esc(ed.value)}</pre>
      <div id="saved-list" style="margin-top:12px"></div>`, foot: `<button class="ghost" data-close>Cancel</button><button data-save>Save</button>` });
    m.$("#saved-list").innerHTML = saved.length ? `<h3>Manage saved</h3>` + saved.map((q) => `<div class="list-row"><span style="flex:1">${esc(q.name)}</span>
      <button class="icon-btn" data-delq="${q.id}">🗑</button></div>`).join("") : "";
    m.el.addEventListener("click", async (e) => { const d = e.target.closest("[data-delq]"); if (!d) return;
      await api(`/api/queries/${d.dataset.delq}`, { method: "DELETE" }); saved = await api("/api/queries"); renderSaved(); d.closest(".list-row").remove(); });
    m.$("[data-save]").onclick = async () => {
      try { await api("/api/queries", { method: "POST", body: { ...readForm(m.el, F), sql: ed.value } }); saved = await api("/api/queries"); renderSaved(); m.close(); toast("Query saved"); }
      catch (e) { showErrors(m.el, e); }
    };
  };
  $("#csv", el).onclick = async () => {
    if (!last) return;
    const res = await api("/api/query/csv", { method: "POST", body: { sql: last.sql }, raw: true }).catch(fail);
    if (res) download(await res.blob(), "query.csv");
  };
  el.addEventListener("click", (e) => {
    const peek = e.target.closest("[data-peek]"), ddl = e.target.closest("[data-ddl]"), col = e.target.closest("[data-col]");
    if (peek) { ed.value = `SELECT *\nFROM ${peek.dataset.peek}\nLIMIT 100;`; run(); }
    if (ddl) { const t = schema.find((x) => x.name === ddl.dataset.ddl); modal({ title: `${t.name} — DDL`, wide: true, body: `<pre class="editor" style="white-space:pre-wrap">${esc(t.sql)}</pre>` }); }
    if (col) { ed.setRangeText(col.dataset.col, ed.selectionStart, ed.selectionEnd, "end"); ed.focus(); }
  });
  if (params.sql) run();
});

// =================================================== import/export/backup
route("/data", async (el, params) => {
  const sections = await api("/api/sections");
  el.innerHTML = `
    <div class="page-head"><div><h1>Import · Export · Backup</h1><p>Move data in and out safely — imports are transactional and can be dry-run first.</p></div></div>
    <div class="grid cols-2" style="margin-bottom:16px">
      <div class="card"><h2>Import</h2>
        <div class="form-grid"><label class="field"><span>Into</span><select id="ent">
          <option value="contacts">Contacts (CSV)</option><option value="vcard">Contacts (vCard .vcf)</option>
          <option value="expenses">Expenses (CSV)</option><option value="codes">Barcodes / QR (CSV)</option>
          ${sections.map((s) => `<option value="section:${esc(s.slug)}">${esc(s.icon)} ${esc(s.name)} (CSV)</option>`).join("")}</select></label>
          <label class="field"><span>File</span><input type="file" id="file" accept=".csv,.tsv,.txt,.vcf"></label></div>
        <div id="mapping" style="margin-top:12px"></div>
        <div class="actions" style="margin-top:12px"><button class="ghost" id="dry" disabled>Dry run</button><button id="go" disabled>Import</button>
          <label class="check small"><input type="checkbox" id="skip"> Import valid rows even if some fail</label></div>
        <div id="report"></div></div>
      <div class="card"><h2>Export</h2>
        <div class="list-row"><span style="flex:1">Contacts</span><a class="btn ghost small" href="/api/export/contacts.csv">CSV</a><a class="btn ghost small" href="/api/export/contacts.vcf">vCard</a></div>
        <div class="list-row"><span style="flex:1">Expenses</span><a class="btn ghost small" href="/api/export/expenses.csv">CSV</a></div>
        <div class="list-row"><span style="flex:1">Barcodes / QR codes</span><a class="btn ghost small" href="/api/export/codes.csv">CSV</a></div>
        <div class="list-row"><span style="flex:1">Picture metadata</span><a class="btn ghost small" href="/api/export/pictures.csv">CSV</a></div>
        ${sections.map((s) => `<div class="list-row"><span style="flex:1">${esc(s.icon)} ${esc(s.name)}</span><a class="btn ghost small" href="/api/export/section:${esc(s.slug)}.csv">CSV</a></div>`).join("")}
        <div class="list-row"><span style="flex:1"><b>Everything</b> <span class="muted small">— logical JSON dump of every table</span></span><a class="btn ghost small" href="/api/export/all.json">JSON</a></div></div></div>
    <div class="grid cols-2">
      <div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><h2 style="margin:0">Backups</h2>
        <div class="actions"><label class="check small"><input type="checkbox" id="bmedia" checked> include pictures</label><button id="bnow">Back up now</button></div></div>
        <p class="muted small">Online snapshot via SQLite's backup API (consistent while the app runs), integrity-checked, zipped with media.
          Restore with <code>datavault restore &lt;zip&gt;</code>.</p><div id="blist"></div></div>
      <div class="card"><h2>Maintenance</h2>
        <div class="list-row"><span style="flex:1">Integrity check<div class="muted small">PRAGMA integrity_check + foreign_key_check</div></span><button class="ghost small" data-mt="integrity">Run</button></div>
        <div class="list-row"><span style="flex:1">Optimize<div class="muted small">FTS merge, PRAGMA optimize, VACUUM</div></span><button class="ghost small" data-mt="optimize">Run</button></div>
        <div class="list-row"><span style="flex:1">Verify media<div class="muted small">re-hash files, find missing/orphaned, rebuild thumbnails</div></span><button class="ghost small" data-mt="verify-media">Run</button></div>
        <div class="list-row"><span style="flex:1">Prune history<div class="muted small">drop change-history entries older than a year</div></span><button class="ghost small" data-mt="prune-audit">Run</button></div>
        <pre id="mt-out" class="editor" style="min-height:0;margin-top:12px" hidden></pre></div></div>
    <div class="card" style="margin-top:16px"><div style="display:flex;justify-content:space-between;align-items:center"><h2 style="margin:0">Recently deleted</h2>
      <span class="muted small">contacts, expenses, codes and section records can be restored exactly as they were</span></div>
      <div id="deleted" style="margin-top:10px"></div></div>`;

  // --- import wizard
  let headerInfo = null;
  const ent = $("#ent", el), file = $("#file", el);
  if (params.import) ent.value = params.import;
  const prepare = async () => {
    headerInfo = null; $("#mapping", el).innerHTML = ""; $("#report", el).innerHTML = "";
    const f = file.files[0];
    $("#dry", el).disabled = $("#go", el).disabled = !f;
    if (!f || ent.value === "vcard") return;
    const fd = new FormData(); fd.append("file", f);
    try { headerInfo = await api(`/api/import/${ent.value}/headers`, { method: "POST", form: fd }); } catch (e) { return fail(e); }
    const opts = (h) => `<option value="">— ignore —</option>` + headerInfo.fields.map((fl) =>
      `<option value="${fl}" ${headerInfo.guess[h] === fl ? "selected" : ""}>${fl === "_full_name" ? "full name (split)" : fl}</option>`).join("");
    $("#mapping", el).innerHTML = `<h3>Column mapping</h3><div class="table-wrap"><table><thead><tr><th>CSV column</th><th>Sample</th><th>Maps to</th></tr></thead><tbody>
      ${headerInfo.headers.map((h, i) => `<tr><td><b>${esc(h)}</b></td><td class="muted small">${esc(headerInfo.sample.map((r) => r[i]).filter(Boolean).slice(0, 2).join(" · "))}</td>
        <td><select data-h="${esc(h)}">${opts(h)}</select></td></tr>`).join("")}</tbody></table></div>`;
  };
  ent.onchange = prepare; file.onchange = prepare;
  const doImport = async (dry) => {
    const fd = new FormData(); fd.append("file", file.files[0]); fd.append("dry_run", dry ? "1" : "0"); fd.append("skip_errors", $("#skip", el).checked ? "1" : "0");
    if (headerInfo) fd.append("mapping", JSON.stringify(Object.fromEntries($$("[data-h]", el).filter((s) => s.value).map((s) => [s.dataset.h, s.value]))));
    try {
      const r = await api(`/api/import/${ent.value}`, { method: "POST", form: fd });
      const ok = !r.errors.length || $("#skip", el).checked;
      $("#report", el).innerHTML = `<div class="${ok ? "card" : "error-box"}" style="margin-top:12px;${ok ? "background:var(--panel-2);box-shadow:none" : ""}">
        <b>${r.dry_run ? "Dry run — nothing was written." : r.rolled_back ? "Rolled back — nothing was written." : "Import committed."}</b><br>
        ${r.total} rows · ${r.dry_run ? "would import" : "imported"} ${r.imported} · ${r.duplicates || 0} duplicates skipped · ${r.errors.length} errors
        ${r.errors.length ? `<ul class="small">${r.errors.slice(0, 30).map((e) => `<li>${e.line ? `line ${e.line}` : `card ${e.card}`}: ${esc(Object.entries(e.errors).map(([k, v]) => `${k} ${v}`).join("; "))}</li>`).join("")}</ul>` : ""}</div>`;
      if (!dry && !r.rolled_back) toast(`Imported ${r.imported} rows`);
    } catch (e) { fail(e); }
  };
  $("#dry", el).onclick = () => doImport(true);
  $("#go", el).onclick = () => doImport(false);

  // --- backups
  const loadBackups = async () => {
    const b = await api("/api/backups");
    $("#blist", el).innerHTML = b.map((x) => `<div class="list-row"><span style="flex:1"><span class="mono small">${esc(x.name)}</span>
      <div class="muted small">${fmtBytes(x.size_bytes)}${x.manifest ? ` · ${x.manifest.contacts} contacts, ${x.manifest.expenses} expenses, ${x.manifest.pictures} pictures` : ""}</div></span>
      <a class="btn ghost small" href="/api/backups/${encodeURIComponent(x.name)}">Download</a></div>`).join("") || `<div class="empty">No backups yet</div>`;
  };
  $("#bnow", el).onclick = async (e) => {
    e.target.disabled = true;
    try { const r = await api("/api/backups", { method: "POST", body: { include_media: $("#bmedia", el).checked } }); toast(`Backup ${r.name} (${fmtBytes(r.size_bytes)})`); loadBackups(); }
    catch (err) { fail(err); } finally { e.target.disabled = false; }
  };
  el.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-mt]"); if (!b) return;
    b.disabled = true; const out = $("#mt-out", el); out.hidden = false; out.textContent = "Running…";
    try { out.textContent = JSON.stringify(await api(`/api/maintenance/${b.dataset.mt}`, { method: "POST" }), null, 2); }
    catch (err) { out.textContent = err.message; } finally { b.disabled = false; }
  });
  const loadDeleted = async () => {
    const d = await api("/api/deleted");
    const names = { contacts: "Contact", expenses: "Expense", codes: "Code", section_records: "Record" };
    $("#deleted", el).innerHTML = d.map((x) => `<div class="list-row"><span class="pill">${names[x.table]}</span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.label)}</span>
      <span class="muted small nowrap">${ago(x.at)}</span><button class="ghost small" data-restore="${x.audit_id}">Restore</button></div>`).join("")
      || `<div class="empty">Nothing deleted recently</div>`;
  };
  $("#deleted", el).addEventListener("click", async (e) => {
    const b = e.target.closest("[data-restore]"); if (!b) return;
    try {
      const r = await api(`/api/deleted/${b.dataset.restore}/restore`, { method: "POST" });
      toast(`Restored${r.notes.length ? ` (${r.notes.join("; ")})` : ""}`); loadDeleted();
    } catch (err) { fail(err); }
  });
  await Promise.all([loadBackups(), loadDeleted()]);
});

// ========================================================= global search
function setupSearch() {
  const input = $("#global-search"), box = $("#search-results");
  const icons = { contact: "☺", picture: "▣", expense: "$", code: "▦", record: "📁", note: "✎", file: "📄", folder: "🗂" };
  let items = [], sel = -1;
  const hrefFor = async (r) => {
    if (r.kind === "contact") return `#/contacts/${r.ref_id}`;
    if (r.kind === "picture") return `#/pictures?open=${r.ref_id}`;
    if (r.kind === "expense") return `#/expenses?edit=${r.ref_id}`;
    if (r.kind === "code") return `#/codes`;
    if (r.kind === "note") return `#/notes/${r.ref_id}`;
    if (r.kind === "folder") return `#/folders/${r.ref_id}`;
    if (r.kind === "file") { window.open(`/files/${r.ref_id}/view`, "_blank", "noopener"); return location.hash; }
    const rec = await api(`/api/records/${r.ref_id}`).catch(() => null);
    if (!rec) return "#/";
    const s = await api(`/api/sections/${rec.section_id}`);
    return `#/s/${s.slug}`;
  };
  const go = async (i) => { const r = items[i]; if (!r) return; box.hidden = true; input.blur(); location.hash = await hrefFor(r); };
  const render = () => {
    box.innerHTML = items.length ? items.map((r, i) => `<a href="javascript:void 0" data-i="${i}" class="${i === sel ? "sel" : ""}">
      <span class="muted">${icons[r.kind] || "•"}</span> <b>${esc(r.title)}</b> <span class="pill">${esc(r.kind)}</span>
      <div class="muted small">${esc(r.snippet).replace(/&lt;mark&gt;/g, "<mark>").replace(/&lt;\/mark&gt;/g, "</mark>")}</div></a>`).join("")
      : `<div class="muted" style="padding:10px">No matches</div>`;
    box.hidden = false;
  };
  input.addEventListener("input", debounce(async () => {
    const q = input.value.trim();
    if (!q) { box.hidden = true; return; }
    items = await api(`/api/search?${qs({ q, limit: 20 })}`).catch(() => []); sel = -1; render();
  }, 180));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { sel = Math.min(items.length - 1, sel + 1); render(); e.preventDefault(); }
    if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); render(); e.preventDefault(); }
    if (e.key === "Enter") go(sel < 0 ? 0 : sel);
    if (e.key === "Escape") { box.hidden = true; input.blur(); }
  });
  box.addEventListener("mousedown", (e) => { const a = e.target.closest("[data-i]"); if (a) { e.preventDefault(); go(+a.dataset.i); } });
  input.addEventListener("blur", () => setTimeout(() => (box.hidden = true), 150));
  input.addEventListener("focus", () => { if (input.value.trim() && items.length) box.hidden = false; });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !e.target.closest("input, textarea, select")) { e.preventDefault(); input.focus(); input.select(); }
  });
}

// ================================================================== boot
function setupTheme() {
  let pref = null;
  try { pref = localStorage.getItem("dv.theme"); } catch { /* storage blocked */ }
  const apply = (t) => { document.documentElement.dataset.theme = t; };
  apply(pref || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  $("#theme-toggle").onclick = () => {
    const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    apply(t); try { localStorage.setItem("dv.theme", t); } catch { /* ignore */ }
  };
}

document.addEventListener("DOMContentLoaded", async () => {
  setupTheme();
  setupSearch();
  $("#menu-btn").onclick = () => $("#sidebar").classList.toggle("open");
  $("#new-section").onclick = () => sectionBuilder(null).catch(fail);
  $("#new-folder").onclick = () => folderForm(null, null, (f) => (location.hash = `#/folders/${f.id}`)).catch(fail);
  window.addEventListener("hashchange", () => { navigate(); loadSectionNav().catch(() => {}); markFolderNav(); });
  await Promise.all([loadSectionNav(), loadFolders()]).catch(fail);
  api("/api/stats").then((s) => ($("#db-meta").textContent = `${fmtBytes(s.db_bytes)} · v${s.schema_version}`)).catch(() => {});
  navigate();
});
