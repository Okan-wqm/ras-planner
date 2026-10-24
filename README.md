# RAS production planner

The trout.xlsx formulas as a Python engine, with a Django dashboard on top.

## Setup

Python 3.12, virtual environment in the project root:

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe manage.py migrate

# optional: seed the catalog (feeds, equipment standards, process templates)
.venv\Scripts\python.exe manage.py loaddata seed_catalog.json

.venv\Scripts\python.exe manage.py runserver
```

Open http://127.0.0.1:8000

PDF export and process snapshots render through Playwright. They use Microsoft Edge by
default; if Edge is missing, install the bundled Chromium fallback once:
`.venv\Scripts\python.exe -m playwright install chromium`

## Layout

| Path | Contents | Excel counterpart |
|---|---|---|
| `ras_engine/params.py` | All constants (label, unit, default) | input cells |
| `ras_engine/formulas.py` | Individual formulas | column formulas |
| `ras_engine/simulate.py` | Day-by-day loop, mass balance, weekly summary | `D. Fry`, `Sayfa10` |
| `ras_engine/biofilter.py` | Biofilter sizing | `Sayfa11` |
| `ras_engine/feed.py` | Feed selection by weight | — |
| `ras_engine/production.py` | Multi-batch plan, peak load per RAS unit, Gantt, harvest calendar | `Sayfa14`, `D. Fry` rows 893+ |
| `planner/` | Django: scenarios, feed table, dashboard, Excel export | — |
| `tests/` | One-to-one comparison with the Excel values | — |

## Tests

```
.venv\Scripts\python.exe -m pytest
```

Regenerate the reference values from Excel with `tools/extract_fixtures.py trout.xlsx`.
`tests/fixtures/MANIFEST.txt` records the SHA-256 hashes of the source workbook and the
generated fixtures; refresh it with `python tools/extract_fixtures.py trout.xlsx --manifest`.

Browser tests (Playwright/Edge) carry the `e2e` marker. Skip them for a fast run:

```
.venv\Scripts\python.exe -m pytest -m "not e2e"
```

## Development

- **Pre-push hook** — `tools/hooks/pre-push` runs the fast regression subset
  (Excel parity, process engine, web) before every push and blocks it on failure.
  Install it into this clone with:

  ```
  cp tools/hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
  ```

  Full suite before a release: `.venv\Scripts\python.exe -m pytest -q`

- **Fixture manifest** — after regenerating fixtures from a changed workbook, refresh the
  SHA-256 record so parity failures point at the right cause:
  `python tools/extract_fixtures.py trout.xlsx --manifest`

- **Daily backup** — `python tools/backup.py` writes `backups/auto-planner-YYYYMMDD.json`
  (dumpdata of the planner app) and `backups/auto-db-YYYYMMDD.sqlite3` (atomic SQLite copy,
  safe while the server runs), then deletes `auto-*` backups older than 7 days. Manual
  backups in `backups/` never carry the `auto-` prefix and are never touched.
  Register the 03:00 daily task (run once, elevated not required):

  ```
  schtasks /create /tn "RASPlannerBackup" /tr "\"J:\İCLOUD\iCloudDrive\suderra\Programlar\.venv\Scripts\python.exe\" \"J:\İCLOUD\iCloudDrive\suderra\Programlar\tools\backup.py\"" /sc daily /st 03:00
  ```

- **Scenario health check** — `python manage.py validate_scenarios` prints a read-only report per
  scenario: unknown or missing constants, orphan unit ids, CAPEX price overrides that match no
  current line, dangling equipment standards and leftover snapshot PNGs. It never writes to the
  database, so it is safe to run against the live data at any time.

## Safety envelope (design margins)

Every design number sits on the SAFE side: demands take max(calculation, rule). The Constants
page has a **Design** group: `design.o2_per_feed` (default **1 kg O₂ per kg feed**), 
`design.co2_per_o2` (1.4), `design.biofilter_min_retention` (4 min, NO₂ risk below) and
`design.tan_knee` (2 mg/L, media volume explodes below). The envelope floors the plan's O₂/CO₂
peaks, the design flow, the diagram basis, the compare acceptance (oxygenation must meet the
design level) and the OPEX oxygen quote — if the calculation asks for more than the rule, the
calculation wins.

**Heating** lives in the same Design group: `design.inlet_water_temp`,
`design.room_temp` and `design.tank_ua_w_per_k` (lumped U·A tank loss, 0 = off). The process
diagrams get a **Heater / heat pump** node (set point, capacity, COP — heat pump 3–5, electric
immersion 1), and OPEX quotes a "Heating before recovery" line: make-up water at 1.163 kWh per
m³ per K plus U·A tank losses, ÷ `heat_cop` — deliberately quoted before any heat recovery, the
honest price of not putting a heat exchanger in. HX NTU sizing is still pending.

## Tank grading

Tanks per batch are set **by fish weight** (03 Units & tanks page), not as one fixed count: fry
start together in one tank and are graded into more as they grow — e.g. up to 2 g → 1 tank,
2–8 g → 3 tanks, above 8 g → all tanks. Tanks are pooled within the RAS unit, so a tank freed by
one batch (harvest or transfer) is immediately taken by the next; the engine reports the busiest
day (tanks actually needed) and the per-step tank size. Fry/fingerling plans should always carry
grading steps — the tanks agent's rule, never leave fish in the maximum tank count from day 1.

## AI assistant

A floating assistant (✦ AI button, Alt+A) sits on every screen and knows the current page: scenario,
plan summary, workflow lamps and the solved process diagrams. For GLM-5.x you can pick the thinking
level (low / high / max) in the panel; keys live on the Library · AI settings page.

Next to the editing tools (`set_param`, `set_stage`, `reset_diagram`, …) it has two read-only
helpers: **`calc`** evaluates safe math (asteval sandbox: min/max/abs/round/sqrt/exp/log10) and
**`preview`** re-runs the whole engine with hypothetical constant overrides (`bio.*` biofilter keys
included) and reports peak feed, O₂/CO₂/TAN, design flow, biomass and biofilter feasibility —
test a change before making it.

The **Library · Agents** page (`/agents/`) shows the agent army as an org chart: the orchestrator
on top, nine specialists below (brief & growth, tanks & hydraulics, oxygenation, biofilter,
microbial, process, energy, economics, proposal writer). Click any card to edit that agent's
context prompt or switch it off; the assistant reaches the same levers via
`read_agent` / `update_agent` / `toggle_agent`.

## Deliberate differences from Excel

- **Density cap:** `min(L/0.32, max density)`. Default 72 kg/m³. Excel freezes at 72.0048 and has a one-row copy on day 214 (N222 = N221).
- **Sayfa11 column V:** Excel uses the flow from the row above (V14 = T14/H13). The engine uses the right row.
- **Sayfa10 scaling:** `simulate.weekly()` takes every 7th day of the single-batch run; the workbook's
  Sayfa10 sheet is additionally scaled ×1.5 in fish count (Sayfa10!D5 = 600,000 vs 'D. Fry'!K9 = 400,000 —
  J5 = D5/'D. Fry'!K9 = 1.5). The engine does not reproduce that scaling.
- **Ozone dose:** per the Excel note, 15 g/kg feed for fish under 5 g and 30 above, switched by weight.
  Excel switches by hand on day 56 (5.82 g); the parity test uses a 5.7 g threshold.
- **Biofilter peak feed:** peak feed ÷ RAS units × 4/3 (Excel note: real feeding increased by 1/3).
- **Growth below Tbase:** the daily length increase is held at zero (fish do not shrink); the TGC path already
  clamps at zero.
- **TGC vs the Excel length curve:** the two growth models are not equivalent. On the defaults, TGC 2.5
  reaches the workbook's 728-day weight (~3,341 g) in ~480 days; the Excel length curve spread over 728 days
  implies TGC ≈ 1.65 (1.6466). The constants page warns when TGC is above 2.0 — verify it for your species.
- **Invalid inputs:** zero/negative constants and efficiencies raise `EngineInputError` (a `ValueError`) with a
  message, instead of dividing by zero or looping on impossible values.
- **Economic FCR:** reported as `—` (None) when the plan is degenerate — no biomass gained, or a feed/biomass
  ratio above 3 (e.g. mortality eats the growth).
- **Feed catalog:** an active feed catalog must cover the whole weight range; a gap raises an error instead of
  silently falling back to the default FCR/protein.

## Economics assumptions

- **Alkalinity (NaHCO₃):** the default 0.552 kg bicarbonate per kg feed follows stoichiometry, not a vendor
  number: 46 g TAN per kg feed (0.092 kg N per kg protein × 0.5 protein) × 7.14 g CaCO₃ per g N × 84/50
  g NaHCO₃ per g CaCO₃-equivalent alkalinity = 0.552 kg/kg. Protein content of your feed moves it.
- **Payroll:** the staffing defaults (1 manager, 2 technicians, 3 labourers and their salaries) are
  placeholders pending a real staffing plan — the OPEX page shows the payroll share of OPEX next to the
  typical 10–20 % band for RAS operations as a sanity check.
- **Discount rate:** the first-years NPV discounts net cash flow at 8 % per year by default; type your own
  rate on the OPEX page.

## Units

| Value | Unit |
|---|---|
| Q O₂, Q TAN, Q CO₂, Q TSS (flow demands) | m³/h |
| Water exchange for NO₃, backflush water | m³/day |
| O₂ and ozone gas (flow meters) | Nl/min (0 °C; the jet page reports Nl/min at 15 °C) |
| Nitrification rate | g TAN / m³ media / h |

Note: in Excel, the `Sayfa10` headers say "liter/day" for NO₃ and backflush, but the values come from
`D. Fry` in m³/day. m³/day is correct.

## License

Copyright (c) 2026 Suderra AS. All rights reserved. This is proprietary software:
the code is visible here for reference only and may not be used, copied, modified or
redistributed without written permission from Suderra AS. See [LICENSE](LICENSE).
