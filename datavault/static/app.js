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
    codes: "#/codes", section_records: null }[a.table_name]);
  el.innerHTML = `
    <div class="page-head"><div><h1>Dashboard</h1><p>Everything in your vault at a glance.</p></div>
      <div class="actions"><a class="btn ghost" href="#/sql">Open SQL console</a><a class="btn" href="#/expenses?new=1">＋ Expense</a></div></div>
    <div class="grid cols-4" style="margin-bottom:16px">
      <a class="card stat" href="#/expenses"><span class="label">Spent in ${esc(monthLabel(sum.month))}</span>
        <span class="value">${money(t.month_cents)}</span>
        <span class="delta ${delta > 0 ? "up" : "down"}">${delta == null ? "&nbsp;" : `${delta > 0 ? "▲" : "▼"} ${Math.abs(delta).toFixed(0)}% vs last month`}</span></a>
      <a class="card stat" href="#/contacts"><span class="label">Contacts</span><span class="value">${c.contacts}</span><span class="muted small">${c.tags} tags</span></a>
      <a class="card stat" href="#/pictures"><span class="label">Pictures</span><span class="value">${c.pictures}</span><span class="muted small">${fmtBytes(c.media_bytes)}</span></a>
      <a class="card stat" href="#/codes"><span class="label">Codes · Records</span><span class="value">${c.codes} · ${c.records}</span><span class="muted small">${c.sections} custom sections</span></a>
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
        <label class="btn ghost small">📷 ${existing?.photo_id ? "Change" : "Add"} photo<input type="file" accept="image/*" hidden id="photo-in"></label>
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
  const state = { q: params.q || "", tag: params.tag || "", sort: "name", selected: id ? +id : null };
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
    const r = await api(`/api/contacts?${qs({ q: state.q, tag: state.tag, sort: state.sort, limit: 500 })}`);
    $("#list", el).innerHTML = r.items.map((c) => `
      <div class="contact-item ${c.id === state.selected ? "active" : ""}" data-id="${c.id}">${avatar(c)}
        <div class="meta"><div><b>${esc(c.full_name)}</b> ${c.favorite ? "<span style='color:#eab308'>★</span>" : ""}</div>
        <div class="muted small">${esc([c.job_title, c.company].filter(Boolean).join(" · ") || c.email || c.phone)}</div></div></div>`).join("")
      || `<div class="empty"><b>No contacts</b>${state.q || state.tag ? "Nothing matches the filter" : "Add your first contact"}</div>`;
    $("#count", el).textContent = `${r.total} contact${r.total === 1 ? "" : "s"}`;
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
        <a class="btn ghost" href="/api/contacts/${c.id}/vcard">Download vCard</a>
        <button class="ghost danger" id="del">Delete</button></div>`;
    $("#edit", el).onclick = () => contactForm(c, async (s) => { await loadList(); loadTags(); showDetail(s.id); });
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

  $("#q", el).addEventListener("input", debounce((e) => { state.q = e.target.value; loadList(); }));
  $("#sort", el).addEventListener("change", (e) => { state.sort = e.target.value; loadList(); });
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
    modal({ title: "Possible duplicates", body: d.length ? `<table><thead><tr><th>Match on</th><th>Value</th><th>Contacts</th></tr></thead><tbody>
      ${d.map((g) => `<tr><td>${esc(g.match_on)}</td><td>${esc(g.value)}</td><td>${JSON.parse(g.ids).map((i) => `<a href="#/contacts/${i}" data-close>#${i}</a>`).join(", ")}</td></tr>`).join("")}
      </tbody></table>` : `<div class="empty"><b>No duplicates</b>No two contacts share an email, phone or name.</div>` });
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
    body: `<div class="lightbox"><a href="/media/${p.id}/file" target="_blank"><img src="/media/${p.id}/file" alt=""></a><div>
      ${formHtml(META, p)}
      <dl class="kv small" style="margin-top:14px;grid-template-columns:90px 1fr">
        <dt>File</dt><dd>${esc(p.original_name)}</dd><dt>Size</dt><dd>${p.width}×${p.height} · ${fmtBytes(p.size_bytes)}</dd>
        <dt>Type</dt><dd>${esc(p.mime)}</dd>${p.camera ? `<dt>Camera</dt><dd>${esc(p.camera)}</dd>` : ""}
        <dt>SHA-256</dt><dd class="mono" style="font-size:10.5px">${esc(p.sha256)}</dd>
        ${used.length ? `<dt>Used by</dt><dd>${used.join("<br>")}</dd>` : ""}</dl></div></div>`,
    foot: `<button class="ghost danger" data-del>Delete</button><a class="btn ghost" href="/media/${p.id}/file?download=1">Download original</a>
      <span class="spacer"></span><button class="ghost" data-close>Close</button><button data-save>Save</button>`,
  });
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
    <label class="dropzone" id="drop"><input type="file" accept="image/*" multiple hidden id="files">
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
        <label class="btn ghost small">🧾 Attach receipt<input type="file" accept="image/*" hidden id="rc-in"></label></div>`,
    foot: `${existing ? `<button class="ghost danger" data-del>Delete</button><span class="spacer"></span>` : ""}
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
    m.close(); toast("Expense deleted"); onSaved(null);
  });
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
    $$("th.sortable", el).forEach((th) => th.textContent = th.textContent.replace(/ [▲▼]$/, "") +
      (state.sort.startsWith(th.dataset.sort) ? (state.sort.endsWith("_asc") ? " ▲" : " ▼") : ""));
  };
  const refresh = () => Promise.all([loadRows(), loadSummary()]).catch(fail);
  const bind = (sel, key) => $(sel, el).addEventListener("input", debounce((e) => { state[key] = e.target.value; state.offset = 0; loadRows().catch(fail); }, 300));
  bind("#q", "q"); bind("#cat", "category_id"); bind("#df", "date_from"); bind("#dt", "date_to"); bind("#mn", "min_amount"); bind("#mx", "max_amount");
  $("#month", el).addEventListener("change", (e) => { state.month = e.target.value; loadSummary().catch(fail); });
  $("#clear", el).onclick = () => { ["#q", "#cat", "#df", "#dt", "#mn", "#mx"].forEach((s) => ($(s, el).value = ""));
    Object.assign(state, { q: "", category_id: "", date_from: "", date_to: "", min_amount: "", max_amount: "", offset: 0 }); loadRows(); };
  $("#prev", el).onclick = () => { state.offset = Math.max(0, state.offset - state.limit); loadRows(); };
  $("#next", el).onclick = () => { state.offset += state.limit; loadRows(); };
  $$("th.sortable", el).forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    state.sort = state.sort === k ? `${k}_asc` : k;
    if (state.sort === "merchant_asc") state.sort = "merchant";
    state.offset = 0; loadRows();
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
        <label class="dropzone" id="scan-drop"><input type="file" accept="image/*" hidden id="scan-file"><b>Drop a photo of a barcode / QR</b> or click — every code in the image is read</label>
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
          <button class="ghost small" data-act="copy">Copy</button><button class="ghost small" data-act="edit">Edit</button><button class="icon-btn" data-act="del" title="Delete">🗑</button></div></div>`;
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
  url: "Link", email: "Email", picture: "Picture", contact: "Contact", code: "Barcode / QR" };

async function loadSectionNav() {
  const secs = await api("/api/sections");
  $("#section-nav").innerHTML = secs.map((s) => `<a href="#/s/${esc(s.slug)}" data-nav="s-${esc(s.slug)}"><i>${esc(s.icon)}</i>${esc(s.name)}
    <span class="muted small" style="margin-left:auto">${s.record_count}</span></a>`).join("")
    || `<div class="muted small" style="padding:4px 10px">No custom sections yet</div>`;
  const cur = location.hash;
  $$("#section-nav a").forEach((a) => a.classList.toggle("active", cur.startsWith(a.getAttribute("href"))));
}

function sectionBuilder(existing) {
  const fields = existing ? existing.fields.map((f) => ({ ...f, options: f.options.join(", ") })) : [{ label: "Name", type: "text", required: true, options: "" }];
  const m = modal({
    title: existing ? `Edit “${existing.name}”` : "New section", wide: true,
    body: `<div class="form-grid" style="grid-template-columns:80px 1fr">
        <label class="field"><span>Icon</span><input id="s-icon" value="${esc(existing?.icon || "📁")}" maxlength="4" style="text-align:center;font-size:18px"></label>
        <label class="field"><span class="req">Name</span><input id="s-name" value="${esc(existing?.name || "")}" placeholder="e.g. Recipes, Vehicles, Warranties"></label>
        <label class="field full"><span>Description</span><input id="s-desc" value="${esc(existing?.description || "")}"></label></div>
      <h3 style="margin-top:18px">Fields <span class="muted small" style="text-transform:none;letter-spacing:0">— drag to reorder</span></h3>
      <div class="field-rows" id="rows"></div>
      <button class="ghost small" id="add" style="margin-top:10px">＋ Add field</button>
      ${existing ? `<p class="muted small">Renaming a field label keeps its data. Removing a field hides its values (they stay in the stored JSON).</p>` : ""}`,
    foot: `${existing ? `<button class="ghost danger" data-del>Delete section</button><span class="spacer"></span>` : ""}
      <button class="ghost" data-close>Cancel</button><button data-save>${existing ? "Save" : "Create section"}</button>`,
  });
  const render = () => {
    m.$("#rows").innerHTML = fields.map((f, i) => `<div class="field-row" draggable="true" data-i="${i}"><span class="grip">⋮⋮</span>
      <input data-k="label" value="${esc(f.label)}" placeholder="Field label">
      <select data-k="type">${Object.entries(FIELD_TYPES).map(([k, v]) => `<option value="${k}" ${f.type === k ? "selected" : ""}>${v}</option>`).join("")}</select>
      <input data-k="options" value="${esc(f.options || "")}" placeholder="${f.type === "select" ? "Choices, comma-separated" : "—"}" ${f.type === "select" ? "" : "disabled"}>
      <label class="check small"><input type="checkbox" data-k="required" ${f.required ? "checked" : ""}> Required</label>
      <button class="icon-btn" data-rm title="Remove">✕</button></div>`).join("");
  };
  render();
  m.$("#rows").addEventListener("input", (e) => {
    const row = e.target.closest("[data-i]"), k = e.target.dataset.k; if (!row || !k) return;
    fields[row.dataset.i][k] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    if (k === "type") render();
  });
  m.$("#rows").addEventListener("click", (e) => { if (e.target.closest("[data-rm]")) { fields.splice(e.target.closest("[data-i]").dataset.i, 1); render(); } });
  let dragFrom = null;
  m.$("#rows").addEventListener("dragstart", (e) => { dragFrom = +e.target.closest("[data-i]").dataset.i; e.target.classList.add("dragging"); });
  m.$("#rows").addEventListener("dragover", (e) => e.preventDefault());
  m.$("#rows").addEventListener("drop", (e) => {
    e.preventDefault(); const to = e.target.closest("[data-i]"); if (!to || dragFrom == null) return;
    const [f] = fields.splice(dragFrom, 1); fields.splice(+to.dataset.i, 0, f); dragFrom = null; render();
  });
  m.$("#add").onclick = () => { fields.push({ label: "", type: "text", required: false, options: "" }); render(); $$("[data-k=label]", m.el).at(-1).focus(); };
  m.$("[data-save]").onclick = async () => {
    const body = { name: m.$("#s-name").value, icon: m.$("#s-icon").value, description: m.$("#s-desc").value, fields };
    try {
      const s = existing ? await api(`/api/sections/${existing.id}`, { method: "PUT", body }) : await api("/api/sections", { method: "POST", body });
      m.close(); toast(existing ? "Section updated" : "Section created"); await loadSectionNav();
      location.hash = `#/s/${s.slug}`; if (existing) navigate();
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
  const F = sec.fields.map((f) => {
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
  sec.fields.filter((f) => f.type === "money").forEach((f) => { if (d[f.key] != null) values[f.key] = (d[f.key] / 100).toFixed(2); });
  const m = modal({
    title: existing ? `Edit ${sec.name} record` : `New ${sec.name} record`, wide: F.length > 5, body: formHtml(F, values),
    foot: `${existing ? `<button class="ghost danger" data-del>Delete</button><span class="spacer"></span>` : ""}<button class="ghost" data-close>Cancel</button><button data-save>Save</button>`,
  });
  m.$("[data-save]").onclick = async () => {
    try {
      const body = readForm(m.el, F);
      existing ? await api(`/api/records/${existing.id}`, { method: "PATCH", body }) : await api(`/api/sections/${sec.id}/records`, { method: "POST", body });
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
  switch (f.type) {
    case "money": return `<span class="num">${money(v)}</span>`;
    case "boolean": return v ? "✓" : `<span class="muted">✗</span>`;
    case "date": return esc(fmtDate(v));
    case "url": return `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v.replace(/^https?:\/\//, "").slice(0, 40))}</a>`;
    case "email": return `<a href="mailto:${esc(v)}">${esc(v)}</a>`;
    case "picture": return `<a href="#/pictures?open=${v}"><img class="thumb" src="/media/${v}/thumb"></a>`;
    case "contact": return `<a href="#/contacts/${v}">contact #${v}</a>`;
    case "code": return `<img src="/api/codes/${v}/image.png" style="height:34px;background:#fff;border-radius:4px">`;
    case "select": return `<span class="pill">${esc(v)}</span>`;
    case "longtext": return `<span title="${esc(v)}">${esc(String(v).slice(0, 80))}${String(v).length > 80 ? "…" : ""}</span>`;
    default: return esc(v);
  }
}

route("/s/([a-z0-9-]+)", async (el, slug) => {
  const state = { q: "", sort: "", desc: false, filters: {} };
  let sec = await api(`/api/sections/${slug}`);
  const filterable = sec.fields.filter((f) => ["select", "boolean"].includes(f.type));
  el.innerHTML = `
    <div class="page-head"><div><h1>${esc(sec.icon)} ${esc(sec.name)}</h1><p>${esc(sec.description || "Custom section")}</p></div>
      <div class="actions"><button class="ghost" id="edit">Edit fields</button><a class="btn ghost" href="/api/export/section:${esc(sec.slug)}.csv">Export CSV</a>
        <button id="new">＋ New record</button></div></div>
    <div class="toolbar"><input type="search" id="q" placeholder="Search records…">
      ${filterable.map((f) => `<select data-filter="${f.key}"><option value="">${esc(f.label)}: any</option>
        ${(f.type === "boolean" ? [["true", "Yes"], ["false", "No"]] : f.options.map((o) => [o, o])).map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("")}</select>`).join("")}
      <span class="muted small" id="count"></span></div>
    <div class="card flush"><div class="table-wrap"><table><thead><tr>${sec.fields.map((f) =>
      `<th class="sortable ${["money", "number"].includes(f.type) ? "right" : ""}" data-sort="${f.key}">${esc(f.label)}</th>`).join("")}
      <th class="sortable" data-sort="updated_at">Updated</th></tr></thead><tbody id="rows"></tbody></table></div></div>
    <p class="muted small" style="margin-top:12px">Stored as JSON documents in <code>section_records</code> — query them in the SQL console with
      <code>json_extract(data, '$.${esc(sec.fields[0]?.key || "field")}')</code>.</p>`;
  const load = async () => {
    const f = Object.fromEntries(Object.entries(state.filters).map(([k, v]) => [`f.${k}`, v]));
    const r = await api(`/api/sections/${sec.id}/records?${qs({ q: state.q, sort: state.sort, desc: state.desc ? 1 : "", ...f, limit: 1000 })}`);
    $("#count", el).textContent = `${r.total} record${r.total === 1 ? "" : "s"}`;
    $("#rows", el).innerHTML = r.items.map((rec) => `<tr class="clickable" data-id="${rec.id}">${sec.fields.map((fl) =>
      `<td class="${["money", "number"].includes(fl.type) ? "num" : ""}">${renderCell(fl, rec.data[fl.key])}</td>`).join("")}
      <td class="muted small nowrap">${ago(rec.updated_at)}</td></tr>`).join("")
      || `<tr><td colspan="${sec.fields.length + 1}"><div class="empty"><b>No records</b>Click “New record” to add one.</div></td></tr>`;
    $$("th.sortable", el).forEach((th) => th.textContent = th.textContent.replace(/ [▲▼]$/, "") + (state.sort === th.dataset.sort ? (state.desc ? " ▼" : " ▲") : ""));
  };
  $("#q", el).addEventListener("input", debounce((e) => { state.q = e.target.value; load(); }));
  $$("[data-filter]", el).forEach((s) => s.addEventListener("change", () => {
    if (s.value) state.filters[s.dataset.filter] = s.value; else delete state.filters[s.dataset.filter]; load().catch(fail);
  }));
  $$("th.sortable", el).forEach((th) => th.addEventListener("click", () => {
    if (state.sort === th.dataset.sort) state.desc = !state.desc; else { state.sort = th.dataset.sort; state.desc = false; }
    load();
  }));
  $("#rows", el).addEventListener("click", async (e) => {
    if (e.target.closest("a")) return;
    const tr = e.target.closest("tr[data-id]");
    if (tr) recordForm(sec, await api(`/api/records/${tr.dataset.id}`), () => { load(); loadSectionNav(); }).catch(fail);
  });
  $("#new", el).onclick = () => recordForm(sec, null, () => { load(); loadSectionNav(); }).catch(fail);
  $("#edit", el).onclick = async () => { sec = await api(`/api/sections/${sec.id}`); sectionBuilder(sec); };
  await load();
});

// ============================================================ SQL console
route("/sql", async (el, params) => {
  const [schema, examples] = await Promise.all([api("/api/schema"), api("/api/query/examples")]);
  let saved = await api("/api/queries");
  let last = null;
  const initial = params.sql || localStorage.getItem("dv.sql") || examples[0].sql;
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
    localStorage.setItem("dv.sql", ed.value);
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
          <option value="expenses">Expenses (CSV)</option><option value="codes">Barcodes / QR (CSV)</option></select></label>
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
        <pre id="mt-out" class="editor" style="min-height:0;margin-top:12px" hidden></pre></div></div>`;

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
  await loadBackups();
});

// ========================================================= global search
function setupSearch() {
  const input = $("#global-search"), box = $("#search-results");
  const icons = { contact: "☺", picture: "▣", expense: "$", code: "▦", record: "📁" };
  let items = [], sel = -1;
  const hrefFor = async (r) => {
    if (r.kind === "contact") return `#/contacts/${r.ref_id}`;
    if (r.kind === "picture") return `#/pictures?open=${r.ref_id}`;
    if (r.kind === "expense") return `#/expenses?edit=${r.ref_id}`;
    if (r.kind === "code") return `#/codes`;
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
  $("#new-section").onclick = () => sectionBuilder(null);
  window.addEventListener("hashchange", () => { navigate(); loadSectionNav().catch(() => {}); });
  await loadSectionNav().catch(fail);
  api("/api/stats").then((s) => ($("#db-meta").textContent = `${fmtBytes(s.db_bytes)} · v${s.schema_version}`)).catch(() => {});
  navigate();
});
