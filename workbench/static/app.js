"use strict";

// Run screen: subscribe to the SSE stream, render events / result / verdict live.
// Replay is idempotent — a reconnect re-sends persisted events from the start, so
// we skip any sequence we've already drawn.

function esc(s) {
  return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function jsonPanel(title, obj, open) {
  const id = "p" + Math.random().toString(36).slice(2);
  const text = JSON.stringify(obj, null, 2);
  return `<details class="panel"${open ? " open" : ""}>
    <summary>${esc(title)} <button class="copy" data-target="${id}">Copy</button></summary>
    <pre id="${id}">${esc(text)}</pre></details>`;
}

function jsonPanelRaw(title, text) {
  const id = "p" + Math.random().toString(36).slice(2);
  return `<details class="panel" open>
    <summary>${esc(title)} <button class="copy" data-target="${id}">Copy</button></summary>
    <pre id="${id}">${esc(text)}</pre></details>`;
}

const ERROR_LABELS = {
  gateway_unreachable: "Gateway unreachable",
  start_rejected: "Start rejected by the Gateway",
  gateway_http_error: "Gateway HTTP error",
  protocol_error: "Protocol violation",
  request_invalid: "Outbound request invalid",
};

function renderError(d) {
  const label = ERROR_LABELS[d.error_kind] || d.error_kind || "Error";
  const gwNote = d.gateway_run_created
    ? "A Gateway run existed when this failed (mid-stream)."
    : "No run was ever created on the Gateway.";
  const box = document.getElementById("run-error");
  box.hidden = false;
  box.innerHTML = `<div class="err-title">✗ Run ended in an error state — ${esc(label)}</div>
    <div class="err-detail">${esc(d.detail || "")}</div>
    <div class="err-gw">${esc(gwNote)}</div>
    ${d.payload_text ? jsonPanelRaw("Raw response from Module 3 (as received)", d.payload_text) : ""}`;
  const st = document.getElementById("run-state");
  if (st) { st.textContent = "error"; st.classList.add("state-error"); }
  // Downstream stages read as "not reached" rather than sitting blank/pending.
  const evBox = document.getElementById("events");
  if (evBox && !evBox.querySelector(".event-row")) {
    evBox.innerHTML = `<div class="kv muted">Not reached — no ExecutionEvents were received.</div>`;
  }
  document.getElementById("result").innerHTML =
    `<div class="kv muted">Not reached — no ValidationResult was received. Module 1 did not fabricate one.</div>`;
  document.getElementById("verdict").innerHTML =
    `<div class="kv">Not reached — no verdict. This run ended in an error state; its effective outcome for
     history and approval is <b>inconclusive</b> — visibly distinct from a Gateway-reported technical
     failure, which returns a valid ValidationResult and derives <b>inconclusive</b> via rule #2.</div>`;
}

function m1Badge(v, label) {
  if (!v) return `<span class="badge wait">${esc(label)}: not validated</span>`;
  return v.passed
    ? `<span class="badge pass">✓ ${esc(label)}</span>`
    : `<span class="badge fail">✗ ${esc(label)} failed (${v.errors.length})</span>`;
}

function buildFlow() {
  const names = ["Inputs", "Module 1 Assembly", "ValidationRequest", "Module 2 Contract",
                 "Gateway", "ExecutionEvents", "ValidationResult", "Module 1 Verdict"];
  const el = document.getElementById("flow");
  if (!el) return;
  names.forEach((n, i) => {
    const node = document.createElement("span");
    node.className = "node";
    node.textContent = (i + 1) + ". " + n;
    node.onclick = () => document.getElementById("stage-" + i)?.scrollIntoView({ behavior: "smooth", block: "start" });
    el.appendChild(node);
    if (i < names.length - 1) {
      const a = document.createElement("span");
      a.className = "arrow"; a.textContent = "→"; el.appendChild(a);
    }
  });
}

document.addEventListener("click", e => {
  if (e.target.classList && e.target.classList.contains("copy")) {
    const pre = document.getElementById(e.target.dataset.target);
    if (pre) navigator.clipboard.writeText(pre.textContent).then(() => {
      const t = e.target.textContent; e.target.textContent = "Copied";
      setTimeout(() => e.target.textContent = t, 900);
    });
  }
});

function renderEvent(box, data) {
  const ev = data.event;
  const div = document.createElement("div");
  div.className = "event-row";
  div.innerHTML = `<div class="kv">#${ev.sequence} <b>${esc(ev.event_type)}</b> — ${esc(ev.message)}
      &nbsp; ${m1Badge(data.m1_validation, "Module 1 validated this ExecutionEvent")}</div>
    ${jsonPanel("ExecutionEvent #" + ev.sequence, ev, false)}`;
  box.appendChild(div);
}

function renderVerdict(v) {
  if (!v) return;
  const word = v.outcome.toUpperCase().replace(/_/g, " ");
  document.getElementById("verdict").innerHTML =
    `<div class="verdict-word">Verdict: ${esc(word)}</div>
     <ul class="reason">${v.reasoning.map(x => `<li class="sym-${x.sym}">${esc(x.text)}</li>`).join("")}</ul>`;
}

// The readable ValidationResult (status, summary, checks, artifacts, diagnosis,
// raw JSON) is rendered SERVER-SIDE (templates/_result.html) so it is covered by
// an automated test; app.js just fetches and injects that fragment on completion.
async function loadResultPanel(runId) {
  try {
    const html = await (await fetch(`/runs/${runId}/panel`)).text();
    document.getElementById("result").innerHTML = html;
  } catch (_) { /* leave the placeholder if the fetch fails */ }
}

document.addEventListener("DOMContentLoaded", () => {
  buildFlow();
  const meta = document.getElementById("run-meta");
  if (!meta) return;
  const runId = JSON.parse(meta.textContent).run_id;
  const box = document.getElementById("events");
  const seen = new Set();
  let firstEvent = true;

  const es = new EventSource(`/runs/${runId}/stream`);

  es.addEventListener("event", e => {
    const data = JSON.parse(e.data);
    if (seen.has(data.event.sequence)) return;   // idempotent replay
    seen.add(data.event.sequence);
    if (firstEvent) { box.innerHTML = ""; firstEvent = false; }
    renderEvent(box, data);
    const st = document.getElementById("run-state");
    if (st) st.textContent = "running";
  });

  es.addEventListener("result", e => {
    const data = JSON.parse(e.data);
    loadResultPanel(runId);
    renderVerdict(data.verdict);
    const st = document.getElementById("run-state"); if (st) st.textContent = "terminal";
  });

  es.addEventListener("run_error", e => renderError(JSON.parse(e.data)));

  es.addEventListener("done", () => es.close());
});
