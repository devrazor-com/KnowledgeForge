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

function renderResult(data) {
  const box = document.getElementById("result");
  let mock = "";
  const mv = data.gateway_result && data.gateway_result.module3_validation;
  if (mv && mv.passed) {
    mock = `<span class="badge pass-muted">Mock-only: Gateway validated the result before sending</span>`;
  }
  box.innerHTML = `${m1Badge(data.result_validation, "Module 1 validated the inbound ValidationResult")} ${mock}
    ${mock ? `<div class="kv muted">The “Mock-only” signal is out of band and not part of the Module 2 contract.</div>` : ""}
    ${jsonPanel("ValidationResult JSON", data.result, true)}`;

  const v = data.verdict;
  if (v) {
    const word = v.outcome.toUpperCase().replace(/_/g, " ");
    document.getElementById("verdict").innerHTML =
      `<div class="verdict-word">Verdict: ${esc(word)}</div>
       <ul class="reason">${v.reasoning.map(r => `<li class="sym-${r.sym}">${esc(r.text)}</li>`).join("")}</ul>`;
  }
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
    renderResult(JSON.parse(e.data));
    const st = document.getElementById("run-state"); if (st) st.textContent = "terminal";
  });

  es.addEventListener("run_error", e => {
    const d = JSON.parse(e.data);
    document.getElementById("result").innerHTML = `<div class="notice-bad">${esc(d.error || "run error")}</div>`;
    const st = document.getElementById("run-state"); if (st) st.textContent = "error";
  });

  es.addEventListener("done", () => es.close());
});
