"""Read-only health report of the scenario blobs: unknown constants, orphan unit ids and CAPEX price
overrides, dangling equipment standards, leftover snapshot PNGs and pending schema migrations.
Nothing is ever written, so it is safe to run against the live database at any time."""
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, connections

from planner.models import EquipmentStandard, Scenario
from planner.schema import CURRENT, MIGRATORS, upgrade
from ras_engine.params import Params

SNAPSHOT_RE = re.compile(r"^(\d+)-")  # snapshots are named "{scenario pk}-{unit}-{hash}.png"


def _schema_column_exists() -> bool:
    """False while migration 0016 has not been applied — the report then reads the rows with the
    column deferred and assumes v1, which is what the migration will stamp them with."""
    connection = connections[DEFAULT_DB_ALIAS]
    with connection.cursor() as cursor:
        try:
            columns = [c.name for c in connection.introspection.get_table_description(
                cursor, Scenario._meta.db_table)]
        except Exception:
            return True  # cannot tell (e.g. table missing): let Django raise the real error later
    return "schema_version" in columns


class Command(BaseCommand):
    help = ("Report the health of every scenario's saved data: unknown or missing constants, orphan "
            "unit ids and CAPEX price overrides, dangling equipment standards, leftover snapshot "
            "PNGs and pending schema migrations. Read-only — nothing is modified or deleted.")

    def handle(self, *args, **options):
        out = self.stdout.write
        known = set(Params().to_flat())  # every flat engine key the app knows today
        migrated = _schema_column_exists()
        try:
            standards = set(EquipmentStandard.objects.values_list("pk", flat=True))
            rows = Scenario.objects.all() if migrated else Scenario.objects.defer("schema_version")
            rows = sorted(rows, key=lambda s: s.pk)
        except Exception as exc:
            out(f"Cannot read the planner tables ({type(exc).__name__}: {exc}) — run migrate first.")
            return
        out(f"validate_scenarios · schema CURRENT={CURRENT} · {len(rows)} scenario(s) · "
            f"migration 0016 {'applied' if migrated else 'PENDING (existing rows will be stamped v1)'}")
        issues = 0
        for sc in rows:
            issues += self._report_scenario(out, sc, known, standards, migrated)
        issues += self._report_snapshots(out, {s.pk for s in rows})
        out(f"Done · {issues} issue(s) found · report only, nothing was modified")

    def _report_scenario(self, out, sc, known, standards, migrated) -> int:
        issues = 0
        if migrated:
            notes = upgrade(sc, save=False) if sc.schema_version != CURRENT else []  # dry run
            head = (f"schema v{sc.schema_version}" if sc.schema_version == CURRENT
                    else f"schema v{sc.schema_version} (CURRENT is {CURRENT})")
        else:  # rows will be stamped v1 by the migration; report what that upgrade would say
            notes = MIGRATORS[1](sc)
            head = "schema v1 (0016 pending; upgrade fires on first load after migrate)"
        out(f'Scenario {sc.pk} · "{sc.name}" · {head}')
        if notes:
            issues += len(notes)
            out(f"  schema upgrade would emit {len(notes)} note(s):")
            for note in notes:
                out(f"    - {note}")

        params = sc.params or {}
        unknown = sorted(k for k in params if k not in known)
        missing = sorted(known - set(params))
        out(f"  params: {len(params)} keys · {len(unknown)} unknown · "
            f"{len(missing)} missing (engine defaults used)")
        if unknown:
            out("    unknown: " + ", ".join(unknown))
        if missing:
            out("    missing: " + ", ".join(missing))
        issues += len(unknown)

        ids = set(sc.unit_ids())
        orphan_process = sorted(set(sc.process or {}) - ids)
        orphan_equip = sorted(set((sc.equipment or {}).get("stages") or {}) - ids)
        if orphan_process or orphan_equip:
            if orphan_process:
                out("    orphan process unit ids (no such RAS unit in the plan): " + ", ".join(orphan_process))
            if orphan_equip:
                out("    orphan equipment unit ids (no such RAS unit in the plan): " + ", ".join(orphan_equip))
        else:
            out("  unit ids: process/equipment keys all match the plan")
        issues += len(orphan_process) + len(orphan_equip)

        overrides = (sc.capex or {}).get("price_overrides") or {}
        if not overrides:
            out("  capex: no price overrides")
        else:
            line_keys, error = self._capex_line_keys(sc)
            if error:
                out(f"  capex: {len(overrides)} price overrides · orphan check skipped ({error})")
            else:
                orphans = {k: v for k, v in overrides.items() if k not in line_keys}
                total = sum(float(v or 0) for v in orphans.values())
                out(f"  capex: {len(overrides)} price overrides · {len(orphans)} match no current "
                    f"CAPEX line (€{total:,.0f})")
                for key in sorted(orphans):
                    out(f"    {key}: €{float(orphans[key]):,.0f}")
                issues += len(orphans)

        dangling = []
        stages = (sc.equipment or {}).get("stages") or {}
        for uid in sorted(stages):
            for cat, sel in sorted((stages[uid] or {}).items()):
                item = str((sel or {}).get("item") or "") if isinstance(sel, dict) else ""
                if item.isdigit() and int(item) not in standards:
                    dangling.append(f"equipment[{uid}].{cat} -> standard #{item}")
        if dangling:
            out("    dangling equipment standards (no longer in the catalog):")
            for entry in dangling:
                out(f"      {entry}")
        else:
            out("  equipment standards: all selections resolve")
        issues += len(dangling)
        return issues

    def _capex_line_keys(self, sc):
        """The current CAPEX line keys — the same computation the CAPEX page runs."""
        try:
            from planner.costing import _capex_data
            from planner.services import plan_result
            got, err = plan_result(sc)
            if err:
                return None, err
            return {ln["key"] for ln in _capex_data(sc, got[1])["lines"]}, None
        except Exception as exc:  # the CAPEX itself cannot run (e.g. a dangling equipment standard)
            return None, f"{type(exc).__name__}: {exc}"

    def _report_snapshots(self, out, pks) -> int:
        directory = Path(settings.BASE_DIR) / "cache" / "snapshots"
        orphans = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.png")):
                m = SNAPSHOT_RE.match(path.name)
                if m and int(m.group(1)) not in pks:
                    orphans.append(path.name)
        if orphans:
            out("Orphan snapshot PNGs (scenario no longer exists; not deleted):")
            for name in orphans:
                out(f"    {name}")
        return len(orphans)
