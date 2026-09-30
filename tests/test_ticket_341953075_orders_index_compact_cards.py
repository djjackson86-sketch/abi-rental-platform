"""
Regression coverage for ABI-341953075.

The orders index metric cards sit above the order list.  They must stay compact
so the list/table keeps as much usable space as possible, while the existing
hide-metrics behaviour remains available to the main profile.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "static" / "css" / "app.css"
TEMPLATE_PATH = ROOT / "templates" / "admin" / "orders" / "index.html"


def _rule(css, selector):
    match = re.search(re.escape(selector) + r"\{([^}]*)\}", css)
    assert match, f"{selector} has no rule in app.css"
    return match.group(1)


def test_orders_metric_cards_use_compact_auto_fit_tracks():
    css = CSS_PATH.read_text(encoding="utf-8")

    metrics = _rule(css, ".orders-metrics")
    assert "auto-fit" in metrics
    assert "minmax(132px,1fr)" in metrics
    assert "gap:8px" in metrics
    assert "margin-bottom:10px" in metrics

    cards = _rule(css, ".orders-metrics>div")
    assert "min-height:66px" in cards
    assert "padding:10px 12px" in cards

    label = _rule(css, ".orders-metrics small")
    assert "font-size:12px" in label
    assert "line-height:1.2" in label

    value = _rule(css, ".orders-metrics b")
    assert "font-size:20px" in value
    assert "line-height:1.1" in value


def test_orders_metrics_hide_toggle_still_targets_the_same_panel():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert 'class="btn ghost metrics-toggle"' in template
    assert 'aria-controls="orders-metrics"' in template
    assert 'id="orders-metrics"' in template
    assert "orders.metrics.hidden" in template
