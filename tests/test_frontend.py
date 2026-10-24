"""Frontend hardening: XSS closure in the occupancy drawing, process editor resilience
(JSON guard + ErrorBoundary) and the save-time node-type whitelist."""
import json
import re
from pathlib import Path

import pytest
from django.urls import reverse

from planner.models import Scenario

pytestmark = pytest.mark.django_db

TEMPLATES = Path(__file__).resolve().parent.parent / "planner" / "templates" / "planner"
XSS_NAME = "<img src=x onerror=window.__xss=1>"


@pytest.fixture
def sc(client):
    client.get("/")
    s = Scenario.objects.get()
    client.post(reverse("production", args=[s.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1", "horizon_unit": "years",
        "stage_name": ["Fry", "Fingerling"], "stage_max": ["2", "5"], "stage_tanks": ["3", "4"], "stage_ret": ["20", "20"]})
    s.refresh_from_db()
    return s


def test_occupancy_stage_name_is_text_not_html(client, sc):
    """A stage name is free-form user input; it must reach the page only through an escaped
    channel (json_script / Django autoescape) and never as raw HTML."""
    resp = client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1", "horizon_unit": "years",
        "stage_name": [XSS_NAME, "Fingerling"], "stage_max": ["2", "5"], "stage_tanks": ["3", "4"],
        "stage_ret": ["20", "20"]})
    assert resp.status_code == 302
    page = client.get(reverse("units", args=[sc.pk]))
    assert page.status_code == 200
    html = page.content.decode()
    # the hostile name never appears as raw markup anywhere on the page
    assert "<img src=x" not in html
    # it does reach the occupancy script — escaped by json_script (\u003C, never <)
    blob = re.search(r'<script id="occ-data"[^>]*>(.*?)</script>', html, re.S).group(1)
    assert "\\u003Cimg src=x onerror=window.__xss=1\\u003E" in blob
    # and in autoescaped template slots it is HTML-escaped text
    assert "&lt;img src=x onerror=window.__xss=1&gt;" in html


def test_units_template_has_no_innerhtml_with_stage_data():
    """Source-level guard: no template literal that interpolates stage data may go through
    innerHTML — textContent/createNode only."""
    src = (TEMPLATES / "units.html").read_text(encoding="utf-8")
    offenders = [line for line in src.splitlines() if "innerHTML" in line and "${st." in line]
    assert offenders == []


def test_process_template_has_error_boundary_and_parse_guard():
    src = (TEMPLATES / "process.html").read_text(encoding="utf-8")
    assert "componentDidCatch" in src
    assert re.search(r"try\s*\{\s*data = JSON\.parse\(document\.getElementById\('editor-data'\)", src, re.S)
    # the fallback cards post the reset URL from the payload
    assert "resetDiagram" in src and "urls.reset" in src


def test_process_save_drops_unregistered_node_types(client, sc):
    editor = client.get(reverse("process", args=[sc.pk])).context["editor"]
    diagram = json.loads(json.dumps(editor["diagram"]))  # deep copy of the default layout
    real = diagram["nodes"][0]
    diagram["nodes"] += [
        {"id": "bogus", "type": "definitely_not_a_type", "position": {"x": 0, "y": 0},
         "data": {"name": "Nope", "spec": {}}},
        "not-even-a-dict",
        {"type": "pump", "position": {"x": 1, "y": 1}, "data": {"name": "Idless"}},  # no id
    ]
    diagram["edges"] += [
        {"id": "to-bogus", "source": real["id"], "sourceHandle": "out", "target": "bogus", "targetHandle": "in"},
        "not-a-dict-either",
    ]
    resp = client.post(editor["urls"]["save"], json.dumps(diagram), content_type="application/json").json()
    assert resp["saved"] and resp["ok"]
    sc.refresh_from_db()
    saved = sc.diagram_of(0)
    assert [n["id"] for n in saved["nodes"] if n["id"] == "bogus"] == []
    assert all(isinstance(n, dict) and n.get("id") for n in saved["nodes"])
    assert not [e for e in saved["edges"] if "bogus" in (e.get("source"), e.get("target"))]
    assert all(isinstance(e, dict) for e in saved["edges"])
    # the untouched part of the diagram survives the whitelist
    assert real["id"] in {n["id"] for n in saved["nodes"]}


def test_all_frontend_assets_are_vendored(client, sc):
    """No page reaches out to a CDN: react/reactflow/chart.js and the fonts are served from
    /static/vendor/ (offline-capable), and Django's static finders can actually resolve them."""
    from django.contrib.staticfiles import finders
    for f in ("vendor/react.production.min.js", "vendor/react-dom.production.min.js",
              "vendor/reactflow.min.js", "vendor/reactflow.css", "vendor/chart.umd.min.js",
              "vendor/fonts.css"):
        assert finders.find(f), f
    for url in (reverse("dashboard", args=[sc.pk]), reverse("process", args=[sc.pk]),
                reverse("biofilter", args=[sc.pk])):
        resp = client.get(url)
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "/static/vendor/" in html, url
        assert "cdn.jsdelivr.net" not in html and "fonts.googleapis.com" not in html, url
