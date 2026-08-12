"""Readable ValidationResult / diagnosis rendering (EVD-2, EVD-5).

The diagnosis is rendered server-side (templates/_result.html) so it can be
covered by an automated test — important because Step 3 will touch app.js again
(Last-Event-ID). This renders the actual template the /runs/{id}/panel endpoint
and the Run screen use, and asserts the rendered structure and content for the
oversized fixture, not CSS: the labelled sections, the expected number of
recommendations and evidence references, the interpretation marking, and that the
raw ValidationResult JSON is still present beneath the readable view.

Renders the template directly (no HTTP/TestClient) to avoid pulling in a new test
dependency.
"""

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from workbench import config

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERSIZED = REPO_ROOT / "tools" / "mock_gateway" / "fixtures" / "result-check-failure-large.json"

LABELS = ["Summary", "Check results", "Artifacts", "Explanation",
          "Technical details", "Recommended package changes", "Evidence references"]


def _render_result(run: dict) -> str:
    env = Environment(loader=FileSystemLoader(str(config.BASE_DIR / "templates")),
                      autoescape=select_autoescape(["html"]))
    return env.get_template("_result.html").render(run=run)


def test_readable_diagnosis_renders():
    result = json.loads(OVERSIZED.read_text(encoding="utf-8"))
    html = _render_result({
        "result": result,
        "result_validation": {"passed": True, "errors": []},
        "gateway_result": {"module3_validation": {"passed": True, "message": "ok"}},
    })

    # 1. every readable section label is present
    for label in LABELS:
        assert label in html, f"missing section: {label}"

    # 2. the expected counts from the oversized fixture render as list items
    recs_seg = html.split("Recommended package changes", 1)[1].split("</ul>", 1)[0]
    assert recs_seg.count("<li>") == 10, "expected 10 recommendations rendered"
    evref_seg = html.split("Evidence references", 1)[1].split("</ul>", 1)[0]
    assert evref_seg.count("<li>") == 10, "expected 10 evidence references rendered"

    # 3. diagnosis is marked as Module 3's interpretation, not fact
    assert "advisory only; it never determines the verdict" in html
    assert "missing_technical_mapping" in html   # category
    assert "Confidence:" in html

    # 4. summary prose and lengthy check output are present, not truncated away
    assert "billing regression suite failed" in html            # from the summary paragraph
    assert "test_invoice_line_priority_support_active" in html   # from the lengthy CHK-TESTS output

    # 5. the raw ValidationResult JSON is still inspectable underneath
    assert "ValidationResult JSON (raw message)" in html
    assert "duration_seconds" in html  # only appears in the raw JSON, not the readable view


def test_diagnosis_absent_result_renders_summary_only():
    # A result with no diagnosis and no checks still renders summary + status cleanly.
    html = _render_result({
        "result": {"run_id": "r", "status": "completed", "summary": "All good.",
                   "check_results": [], "diagnosis": None, "artifacts": [], "duration_seconds": 1.0},
        "result_validation": {"passed": True, "errors": []},
        "gateway_result": None,
    })
    assert "Summary" in html and "All good." in html
    assert "Diagnosis" not in html
    assert "ValidationResult JSON (raw message)" in html
