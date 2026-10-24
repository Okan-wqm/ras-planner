"""Daily backup: planner app dumpdata + atomic SQLite copy, with 7-day rotation.

Writes only ``backups/auto-*`` files; manual backups in ``backups/`` (which never
carry the ``auto-`` prefix) are never read, rotated, or deleted.

Run by hand::

    python tools/backup.py

or daily at 03:00 via Task Scheduler (see README "Development").
"""
import datetime as dt
import sys
import time
from pathlib import Path

import django
import os

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rasplan.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402

BACKUP_DIR = Path(settings.BASE_DIR) / "backups"
KEEP_DAYS = 7
# Only these two patterns are ever rotated — the auto- prefix is what keeps the
# manual backups (db_before_restructure_*.sqlite3, scenario1_*.json, ...) safe.
ROTATION_PATTERNS = ("auto-planner-*.json", "auto-db-*.sqlite3")


def dump_planner(target: Path) -> None:
    call_command("dumpdata", "planner", indent=2, output=str(target))


def copy_database(source, target: Path) -> None:
    """Atomic copy: the sqlite3 backup API works while the dev server is running."""
    import sqlite3

    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def rotate(cutoff: float) -> list:
    """Delete our own auto-* backups older than KEEP_DAYS; nothing else."""
    deleted = []
    for pattern in ROTATION_PATTERNS:
        for f in BACKUP_DIR.glob(pattern):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                deleted.append(f.name)
    return deleted


def main() -> int:
    BACKUP_DIR.mkdir(exist_ok=True)
    today = dt.date.today().strftime("%Y%m%d")
    json_path = BACKUP_DIR / f"auto-planner-{today}.json"
    db_path = BACKUP_DIR / f"auto-db-{today}.sqlite3"

    dump_planner(json_path)
    copy_database(settings.DATABASES["default"]["NAME"], db_path)

    cutoff = time.time() - KEEP_DAYS * 86400
    deleted = rotate(cutoff)

    print(f"backup: wrote {json_path.name} + {db_path.name}, "
          f"deleted {len(deleted)} aged file(s){': ' + ', '.join(deleted) if deleted else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
