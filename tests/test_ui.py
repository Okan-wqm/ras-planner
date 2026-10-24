"""Browser tests of the process editor (Playwright driving the installed Microsoft Edge).
Skipped when Playwright or Edge is not available."""
import os

import pytest

playwright = pytest.importorskip("playwright.sync_api")
# Playwright's sync API runs an event loop in this thread; the DB calls here are still synchronous
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

pytestmark = pytest.mark.e2e


@pytest.fixture
def page(live_server):
    try:
        pw = playwright.sync_playwright().start()
        browser = pw.chromium.launch(channel="msedge", headless=True)
    except Exception as exc:  # no Edge on this machine
        pytest.skip(f"Edge not available: {exc}")
    pg = browser.new_page(viewport={"width": 1800, "height": 1100})
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    browser.close()
    pw.stop()


@pytest.fixture
def empty_diagram(transactional_db, live_server, client):
    from django.urls import reverse
    from planner.models import Scenario
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1", "horizon_unit": "years",
        "stage_name": ["Fry"], "stage_max": ["5"], "stage_tanks": ["4"], "stage_ret": ["20"]})
    sc.refresh_from_db()
    sc.process = {sc.unit_id(0): {"nodes": [], "edges": []}}
    sc.save()
    return sc


def saved(sc):
    sc.refresh_from_db()
    return sc.diagram_of(0)


def test_gas_line_route_drag_and_pipe_type(page, live_server, empty_diagram):
    sc = empty_diagram
    sc.process = {sc.unit_id(0): {"nodes": [
        {"id": "p", "type": "pump", "position": {"x": 0, "y": 0}, "data": {"name": "Pump", "spec": {"flow_m3h": 100}}},
        {"id": "oz", "type": "ozone", "position": {"x": 300, "y": 0}, "data": {"name": "Contactor", "spec": {}}},
        {"id": "g", "type": "ozone_generator", "position": {"x": 300, "y": -260}, "data": {"name": "Generator", "spec": {"output_g_h": 40}}},
        {"id": "d", "type": "discharge", "position": {"x": 650, "y": 200}, "data": {"name": "Out", "spec": {}}}],
        "edges": [{"id": "e1", "source": "p", "sourceHandle": "out", "target": "oz", "targetHandle": "in"},
                  {"id": "e2", "source": "oz", "sourceHandle": "out", "target": "d", "targetHandle": "in"}]}}
    sc.save()
    page.goto(f"{live_server.url}/scenario/{sc.pk}/process/")
    page.wait_for_selector(".pipe-label")

    # 1. gas line: generator's gas port -> contactor's gas port
    page.drag_and_drop("[data-id='g'] .react-flow__handle.gas", "[data-id='oz'] .react-flow__handle.gas")
    page.wait_for_timeout(2500)
    gas = [e for e in saved(sc)["edges"] if e["source"] == "g"]
    assert len(gas) == 1 and gas[0]["sourceHandle"] == "gas" and gas[0]["targetHandle"] == "gas"
    assert page.locator(".pipe-label:has-text('O₃ gas')").count() == 1
    # a water outlet cannot be connected to the gas port
    page.drag_and_drop("[data-id='p'] .react-flow__handle.source", "[data-id='oz'] .react-flow__handle.gas")
    page.wait_for_timeout(300)
    assert page.locator(".react-flow__edge").count() == 3

    # 2. drag the label of pump -> contactor to move the pipe's bend, double-click resets it
    label = page.locator(".pipe-label").filter(has_text="100.0 m³/h").first
    box = label.bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + 160, steps=8)
    page.mouse.up()
    page.wait_for_timeout(2500)
    e1 = next(e for e in saved(sc)["edges"] if e["id"] == "e1")
    assert e1.get("cx") is not None and e1.get("cy") is not None
    page.locator(".pipe-label.moved").first.dblclick()
    page.wait_for_timeout(2500)
    assert "cx" not in next(e for e in saved(sc)["edges"] if e["id"] == "e1")

    # 2b. grab the pipe on its line (not the label) and drag it
    pt = page.evaluate("""() => {
        const p = document.querySelector("[data-testid='rf__edge-e1'] .pipe-hit");
        const q = p.getPointAtLength(p.getTotalLength() * 0.2);
        const m = p.getScreenCTM();
        return {x: q.x * m.a + m.e, y: q.y * m.d + m.f};
    }""")
    page.mouse.move(pt["x"], pt["y"])
    page.mouse.down()
    page.mouse.move(pt["x"] + 40, pt["y"] + 120, steps=8)
    page.mouse.up()
    page.wait_for_timeout(2500)
    first = next(e for e in saved(sc)["edges"] if e["id"] == "e1")
    assert first.get("cx") is not None

    # 2c. a purely vertical drag moves the pipe up/down (not only left/right)
    lab = page.locator("[data-testid='rf__edge-e1'] ~ * .pipe-label.moved, .pipe-label.moved").first.bounding_box()
    page.mouse.move(lab["x"] + 6, lab["y"] + lab["height"] / 2)
    page.mouse.down()
    page.mouse.move(lab["x"] + 6, lab["y"] + lab["height"] / 2 - 150, steps=10)
    page.mouse.up()
    page.wait_for_timeout(2500)
    second = next(e for e in saved(sc)["edges"] if e["id"] == "e1")
    assert second["cy"] < first["cy"] - 50 and abs(second["cx"] - first["cx"]) < 10
    # and the drawn pipe really passes through the new height
    top = page.evaluate("() => document.querySelector(\"[data-testid='rf__edge-e1'] .pipe-hit\").getBBox().y")
    assert top <= second["cy"] + 1

    # 3. mark contactor -> discharge as cleaning water (a plain click on the line selects it)
    pt = page.evaluate("""() => {
        const p = document.querySelector("[data-testid='rf__edge-e2'] .pipe-hit");
        const q = p.getPointAtLength(p.getTotalLength() * 0.2);
        const m = p.getScreenCTM();
        return {x: q.x * m.a + m.e, y: q.y * m.d + m.f};
    }""")
    page.mouse.click(pt["x"], pt["y"])
    page.locator("label.fld:has-text('Pipe type') select").select_option("cleaning")
    page.wait_for_timeout(2500)
    assert next(e for e in saved(sc)["edges"] if e["id"] == "e2")["service"] == "cleaning"
    assert page.errors == []


def test_degasser_has_four_inlets(page, live_server, empty_diagram):
    page.goto(f"{live_server.url}/scenario/{empty_diagram.pk}/process/")
    page.locator(".palette button:has-text('CO₂ degasser')").click()
    ids = [h.get_attribute("data-handleid") for h in page.locator(".eq .react-flow__handle.target").all()]
    assert ids == ["in", "in2", "in3", "in4"]
    # stacked top to bottom on the left
    ys = [h.bounding_box()["y"] for h in page.locator(".eq .react-flow__handle.target").all()]
    assert ys == sorted(ys) and len(set(ys)) == 4
    assert page.errors == []


def test_incubator_water_in_top_out_bottom(page, live_server, empty_diagram):
    page.goto(f"{live_server.url}/scenario/{empty_diagram.pk}/process/")
    page.locator(".palette button:has-text('Hatchery incubator')").click()
    node = page.locator(".eq").first
    inlet = node.locator(".react-flow__handle.target")
    outlet = node.locator(".react-flow__handle.source")
    assert inlet.count() == 1 and outlet.count() == 1
    assert "react-flow__handle-top" in inlet.get_attribute("class")
    assert "react-flow__handle-bottom" in outlet.get_attribute("class")
    assert inlet.bounding_box()["y"] < outlet.bounding_box()["y"]
    assert page.errors == []


def test_sump_and_sand_filter_ports(page, live_server, empty_diagram):
    page.goto(f"{live_server.url}/scenario/{empty_diagram.pk}/process/")
    page.locator(".palette button:has-text('Sump')").click()
    sump = page.locator(".eq").first
    assert [h.get_attribute("data-handleid") for h in sump.locator(".react-flow__handle.target").all()] == ["in", "in2", "in3"]
    outs = {h.get_attribute("data-handleid"): h.get_attribute("class") for h in sump.locator(".react-flow__handle.source").all()}
    assert set(outs) == {"out", "out2", "out3", "drain"}
    assert all("handle-right" in outs[p] for p in ("out", "out2", "out3")) and "handle-bottom" in outs["drain"]
    page.locator(".palette button:has-text('Sand filter')").click()
    sf = page.locator(".eq").nth(1)
    assert {h.get_attribute("data-handleid") for h in sf.locator(".react-flow__handle.source").all()} == {"out", "backwash"}
    assert page.errors == []


def test_pump_standby_and_frequency_drive(page, live_server, empty_diagram):
    sc = empty_diagram
    page.goto(f"{live_server.url}/scenario/{sc.pk}/process/")
    page.locator(".palette button:has-text('Pump')").first.click()
    assert page.locator("label.fld:has-text('Operating frequency')").count() == 0   # only with a VFD
    page.locator("label.fld:has-text('Frequency drive') select").select_option("true")
    page.locator("label.fld:has-text('Operating frequency') input").fill("40")
    page.wait_for_timeout(800)
    assert page.locator(".pbadge.vfd").inner_text() == "VFD 40 Hz"
    assert "80.0 m³/h · 40.0 Hz" in page.locator(".eq .m").first.inner_text()
    page.locator("label.fld:has-text('Running') select").select_option("false")
    page.wait_for_timeout(800)
    assert page.locator(".pbadge.off").count() == 1 and page.locator(".eq.off").count() == 1
    page.wait_for_timeout(2000)
    sc.refresh_from_db()
    spec = sc.diagram_of(0)["nodes"][0]["data"]["spec"]
    assert spec["active"] is False and spec["vfd"] is True and float(spec["frequency_hz"]) == 40
    assert page.errors == []


def junction_roles(page):
    return {h.get_attribute("data-handleid"): ("in" if "target" in h.get_attribute("class") else "out")
            for h in page.locator(".junc .react-flow__handle").all()}


@pytest.mark.parametrize("size", [(1366, 768), (1920, 1080)])
def test_connection_point_is_visible_in_palette(page, live_server, empty_diagram, size):
    """The connection point must be reachable without scrolling the palette on common screens."""
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.goto(f"{live_server.url}/scenario/{empty_diagram.pk}/process/")
    btn = page.locator(".palette button:has-text('Connection point')")
    palette = page.locator(".palette").bounding_box()
    box = btn.bounding_box()
    assert box["y"] >= palette["y"] and box["y"] + box["height"] <= palette["y"] + palette["height"]
    btn.click()  # click also adds it
    assert page.locator(".junc").count() == 1


def test_connection_point_ports_switch_from_specification(page, live_server, empty_diagram):
    sc = empty_diagram
    page.goto(f"{live_server.url}/scenario/{sc.pk}/process/")
    page.drag_and_drop(".palette button:has-text('Connection point')", ".react-flow__pane",
                       target_position={"x": 500, "y": 300})
    page.locator(".junc").first.click()
    assert junction_roles(page) == {"top": "in", "right": "out", "bottom": "out", "left": "in"}
    # outlets show the solver's default (rest), not 0 %
    assert page.locator(".outlet select").first.input_value() == "rest"
    page.locator("label.fld:has-text('Bottom') select").select_option("in")
    page.locator("label.fld:has-text('Left') select").select_option("out")
    assert junction_roles(page) == {"top": "in", "right": "out", "bottom": "in", "left": "out"}
    page.wait_for_timeout(2500)  # autosave
    sc.refresh_from_db()
    roles = [n["data"]["roles"] for n in sc.diagram_of(0)["nodes"] if n["type"] == "junction"]
    assert roles == [{"top": "in", "right": "out", "bottom": "in", "left": "out"}]
    assert page.errors == []


def test_chiller_and_heat_exchangers(page, live_server, empty_diagram):
    sc = empty_diagram
    page.goto(f"{live_server.url}/scenario/{sc.pk}/process/")
    page.locator(".palette button:has-text('Chiller')").click()
    ch = page.locator(".eq").first
    assert [h.get_attribute("data-handleid") for h in ch.locator(".react-flow__handle.target").all()] == ["in"]
    assert [h.get_attribute("data-handleid") for h in ch.locator(".react-flow__handle.source").all()] == ["out"]
    for i, label in enumerate(["Heat exchanger (plate)", "Heat exchanger (shell & tube)"], start=1):
        page.locator(f".palette button:has-text('{label}')").click()
        hx = page.locator(".eq").nth(i)
        ins = hx.locator(".react-flow__handle.target").all()
        assert [h.get_attribute("data-handleid") for h in ins] == ["in", "in2"]
        assert [h.get_attribute("title") for h in ins] == ["Process in", "Utility in"]
        assert [h.get_attribute("data-handleid") for h in hx.locator(".react-flow__handle.source").all()] == ["out", "out2"]
    page.wait_for_timeout(2500)
    sc.refresh_from_db()
    assert sorted(n["type"] for n in sc.diagram_of(0)["nodes"]) == ["chiller", "hx_plate", "hx_tube"]
    assert page.errors == []


def test_reservoir_and_movable_biofilter_ports(page, live_server, empty_diagram):
    sc = empty_diagram
    page.goto(f"{live_server.url}/scenario/{sc.pk}/process/")
    page.locator(".palette button:has-text('Water reservoir')").click()
    res = page.locator(".eq").first
    assert [h.get_attribute("data-handleid") for h in res.locator(".react-flow__handle.target").all()] == ["in", "in2"]
    assert {h.get_attribute("data-handleid") for h in res.locator(".react-flow__handle.source").all()} == {"out", "out2", "drain"}
    page.locator(".palette button:has-text('Biofilter (MBBR)')").click()
    bio = page.locator(".eq").nth(1)
    assert bio.locator(".sym").bounding_box()["height"] > 170   # taller than a tank
    out = bio.locator(".react-flow__handle[data-handleid='out']")
    assert "handle-right" in out.get_attribute("class")
    page.locator("label.fld:has-text('Outlet position') select").select_option("left-bottom")
    page.locator("label.fld:has-text('Inlet position') select").select_option("top-left")
    page.wait_for_timeout(500)
    assert "handle-left" in out.get_attribute("class")
    assert "handle-top" in bio.locator(".react-flow__handle[data-handleid='in']").get_attribute("class")
    page.wait_for_timeout(2500)
    sc.refresh_from_db()
    bio_saved = next(n for n in sc.diagram_of(0)["nodes"] if n["type"] == "biofilter")
    assert bio_saved["data"]["portPos"] == {"in": "top-left", "out": "left-bottom"}
    assert page.errors == []


def test_undo_redo_and_backspace_does_not_delete(page, live_server, empty_diagram):
    sc = empty_diagram
    page.goto(f"{live_server.url}/scenario/{sc.pk}/process/")
    page.locator(".palette button:has-text('Chiller')").click()
    page.locator(".palette button:has-text('UV unit')").click()
    assert page.locator(".eq").count() == 2
    page.locator(".eq").first.click()
    page.keyboard.press("Backspace")              # typing in the canvas must not delete equipment
    page.wait_for_timeout(300)
    assert page.locator(".eq").count() == 2
    page.keyboard.press("Control+z")
    page.wait_for_timeout(300)
    assert page.locator(".eq").count() == 1
    page.keyboard.press("Control+z")
    page.wait_for_timeout(300)
    assert page.locator(".eq").count() == 0
    page.locator("button:has-text('Redo')").click()
    page.wait_for_timeout(300)
    assert page.locator(".eq").count() == 1
    page.wait_for_timeout(2500)                   # the undone state is what gets saved
    sc.refresh_from_db()
    assert [n["type"] for n in sc.diagram_of(0)["nodes"]] == ["chiller"]
    assert page.errors == []


def test_jet_oxygenators_in_the_palette(page, live_server, empty_diagram):
    page.goto(f"{live_server.url}/scenario/{empty_diagram.pk}/process/")
    for i, label in enumerate(["Jet oxygenator (rectangular)", "Jet oxygenator (round)"]):
        page.locator(f".palette button:has-text('{label}')").click()
        node = page.locator(".eq").nth(i)
        assert [h.get_attribute("data-handleid") for h in node.locator(".react-flow__handle.target").all()] == ["in"]
        assert {h.get_attribute("data-handleid") for h in node.locator(".react-flow__handle.source").all()} == {"out", "drain"}
    assert page.locator("label.fld:has-text('Gas compartments')").count() == 1
    assert page.errors == []
