// Minimal ML Sentry reviewer UI. All values are rendered with textContent (never innerHTML).
"use strict";

const main = document.getElementById("main");
const tokenInput = document.getElementById("token");
const KEY = "mlSentryToken";

function token() {
  try { return sessionStorage.getItem(KEY) || ""; } catch (_) { return ""; }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "onclick") node.addEventListener("click", v);
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    node.append(child instanceof Node ? child : document.createTextNode(String(child ?? "")));
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Authorization": `Bearer ${token()}`, "Content-Type": "application/json" },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = body.error || {};
    throw new Error(`${response.status} ${err.code || ""}: ${err.message || response.statusText}`);
  }
  return body;
}

function show(...nodes) { main.replaceChildren(...nodes); }
function fail(error) { show(el("p", { class: "error" }, error.message)); }
function when(iso) { return iso ? new Date(iso).toLocaleString() : "—"; }

async function approvalsView() {
  const approvals = await api("/api/v1/approvals/pending");
  const rows = approvals.map((a) => el("tr", {},
    el("td", {}, el("a", { href: `#approval/${a.id}` }, a.gate)),
    el("td", {}, a.spec.model_name),
    el("td", { class: "pending" }, a.status),
    el("td", {}, when(a.expires_at)),
    el("td", {}, el("a", { href: `#incident/${a.incident_id}` }, "incident"))));
  show(el("h2", {}, "Pending approvals"),
    rows.length ? el("table", {}, el("tr", {}, ...["Gate", "Model", "Status", "Expires", ""].map((h) => el("th", {}, h))), ...rows)
                : el("p", { class: "muted" }, "Nothing is waiting for a decision."));
}

async function approvalView(id) {
  const a = await api(`/api/v1/approvals/${encodeURIComponent(id)}`);
  const note = el("textarea", { placeholder: "Decision note (optional)", maxlength: "1000" });
  const result = el("p");
  const decideWith = (decision) => async () => {
    if (!confirm(`${decision.toUpperCase()} this ${a.gate} request?`)) return;
    try {
      const updated = await api(`/api/v1/approvals/${encodeURIComponent(id)}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision, spec_digest: a.spec_digest, note: note.value || null }),
      });
      result.className = updated.status;
      result.textContent = `Recorded: ${updated.status} by ${updated.decided_by}`;
    } catch (e) { result.className = "error"; result.textContent = e.message; }
  };
  const actions = a.status === "pending"
    ? el("div", { class: "actions" }, note,
        el("button", { onclick: decideWith("approve") }, "Approve exactly this"),
        el("button", { onclick: decideWith("reject") }, "Reject"))
    : el("p", { class: a.status }, `Decided: ${a.status} by ${a.decided_by || "system"} at ${when(a.decided_at)}`);
  show(el("h2", {}, `${a.gate} approval`),
    el("p", {}, "Status: ", el("span", { class: a.status }, a.status), ` · expires ${when(a.expires_at)} · policy ${a.policy_version}`),
    el("p", { class: "muted" }, `Spec digest ${a.spec_digest}. Approving authorizes only this exact spec.`),
    el("pre", {}, JSON.stringify(a.spec, null, 2)), actions, result);
}

async function incidentsView() {
  const page = await api("/api/v1/incidents?limit=50");
  const rows = page.items.map((i) => el("tr", {},
    el("td", {}, el("a", { href: `#incident/${i.id}` }, when(i.created_at))),
    el("td", {}, `${i.model_name} ${i.model_version}`),
    el("td", { class: i.severity }, i.severity),
    el("td", { class: i.status }, i.status),
    el("td", {}, i.workflow_status || "—")));
  show(el("h2", {}, `Incidents (${page.total})`),
    el("table", {}, el("tr", {}, ...["Opened", "Model", "Severity", "Status", "Workflow"].map((h) => el("th", {}, h))), ...rows));
}

async function incidentView(id) {
  const i = await api(`/api/v1/incidents/${encodeURIComponent(id)}`);
  const audit = await api(`/api/v1/incidents/${encodeURIComponent(id)}/audit`);
  show(el("h2", {}, `Incident: ${i.model_name} ${i.model_version}`),
    el("p", {}, el("span", { class: i.severity }, i.severity), ` · ${i.status} · workflow ${i.workflow_status || "—"}`),
    el("p", {}, i.summary),
    el("h3", {}, "Audit timeline"),
    el("table", {}, ...audit.map((a) => el("tr", {}, el("td", {}, when(a.created_at)), el("td", {}, a.action), el("td", {}, a.outcome), el("td", { class: "muted" }, a.actor)))),
    el("h3", {}, "Decision"), el("pre", {}, JSON.stringify(i.decision, null, 2)));
}

async function route() {
  if (!token()) return;
  const [view, id] = location.hash.slice(1).split("/");
  try {
    if (view === "approval" && id) await approvalView(id);
    else if (view === "incident" && id) await incidentView(id);
    else if (view === "incidents") await incidentsView();
    else await approvalsView();
  } catch (e) { fail(e); }
}

document.getElementById("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  try { sessionStorage.setItem(KEY, tokenInput.value.trim()); } catch (_) { /* storage blocked */ }
  tokenInput.value = "";
  route();
});
document.getElementById("forget").addEventListener("click", () => {
  try { sessionStorage.removeItem(KEY); } catch (_) { /* ignore */ }
  show(el("p", {}, "Token forgotten."));
});
window.addEventListener("hashchange", route);
route();
