"""Versioning of a Scenario's JSON blobs. The blobs evolve with the app; instead of rewriting a
customer's saved values (decision DG-1), old rows keep their data and are flagged for review:
migrators only LOOK and report. planner.migrations.0016 stamps existing rows with version 1."""
import logging

logger = logging.getLogger("planner")

CURRENT = 2


def _v1_to_v2(scenario) -> list[str]:
    """Review notes for rows written before the O₂ split: gas.o2_per_feed then covered the fish and
    nitrification together, so a value from that era needs a human look. Read-only, changes nothing."""
    notes = []
    value = (scenario.params or {}).get("gas.o2_per_feed")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) >= 0.8:
        note = (f"v1 semantics: gas.o2_per_feed={value} predates the fish/nitrification O₂ split "
                f"(current default 0.35) — review this value")
        notes.append(note)
        logger.warning("scenario %s (%s): %s", scenario.pk, scenario.name, note)
    overrides = ((scenario.capex or {}).get("price_overrides") or {})
    if overrides:
        # orphan detection (overrides that match no current line) is the validate_scenarios command's
        # job; real cleanup is Phase 6 — this stays a cheap count
        notes.append(f"capex: {len(overrides)} price overrides stored")
    return notes


MIGRATORS = {1: _v1_to_v2}


def upgrade(scenario, save: bool = True) -> list[str]:
    """Bring a scenario up to the current schema version and return its review notes. Blobs are never
    modified — only schema_version is saved, and updated_at is untouched (explicit update_fields).
    save=False is the dry run used by validate_scenarios."""
    notes: list[str] = []
    if scenario.schema_version > CURRENT:
        note = (f"schema_version={scenario.schema_version} is newer than this code knows ({CURRENT}); "
                f"row left untouched — was the database opened by a newer app?")
        notes.append(note)
        logger.warning("scenario %s (%s): %s", scenario.pk, scenario.name, note)
        return notes
    start = scenario.schema_version
    while scenario.schema_version < CURRENT:  # migrators run in ascending version order
        step = MIGRATORS.get(scenario.schema_version)
        if step is None:
            note = (f"no migrator from schema v{scenario.schema_version} to v{CURRENT}; "
                    f"schema_version left unchanged")
            notes.append(note)
            logger.warning("scenario %s (%s): %s", scenario.pk, scenario.name, note)
            return notes
        notes.extend(step(scenario))
        scenario.schema_version += 1
    if save and scenario.pk is not None and scenario.schema_version != start:
        scenario.save(update_fields=["schema_version"])
    return notes
