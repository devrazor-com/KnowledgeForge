"use strict";

// Pure, DOM-free dedup seam for the live ExecutionEvent stream.
//
// Defence-in-depth: with the SSE resume cursor (Last-Event-ID) the server no longer
// re-sends acknowledged ExecutionEvents on a native reconnect, so in normal
// operation there is no replay overlap for this to hide. It remains as a safeguard:
// if a duplicate sequence nevertheless reaches the JavaScript rendering path (an
// unexpected replay, a future defect), each sequence is still rendered only once.
//
// Kept as a separate DOM-free module so a Node subprocess test can exercise the
// REAL function (not a reimplementation) without a browser. It is a browser global
// AND a CommonJS export.
function shouldRenderEvent(seen, sequence) {
  if (seen.has(sequence)) return false;   // already rendered — suppress the duplicate
  seen.add(sequence);
  return true;                            // first time — render it
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { shouldRenderEvent };   // for the Node-based regression test
}
