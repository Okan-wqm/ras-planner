"""The fixtures manifest: the parity inputs must be exactly the workbook they claim to come from.

tests/fixtures/MANIFEST.txt is written by tools/extract_fixtures.py; if anyone regenerates,
hand-edits or truncates trout.xlsx / dfry.json / biofilter.json, the parity suite would silently
compare against something else. This test recomputes the hashes."""
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest():
    lines = (FIX / "MANIFEST.txt").read_text(encoding="utf-8").strip().splitlines()
    manifest = {}
    for line in lines:
        key, _, value = line.partition(":")
        manifest[key.strip()] = value.strip()
    return manifest


def test_manifest_parses():
    manifest = _manifest()
    assert manifest, "MANIFEST.txt is empty"
    for key in ("source_workbook", "source_sha256", "dfry.json_sha256", "biofilter.json_sha256"):
        assert manifest.get(key), f"MANIFEST.txt is missing {key}"


def test_manifest_hashes_match():
    manifest = _manifest()
    assert _sha256(FIX / "dfry.json") == manifest["dfry.json_sha256"]
    assert _sha256(FIX / "biofilter.json") == manifest["biofilter.json_sha256"]


def test_manifest_source_workbook_hash():
    """The workbook is not published with the repo; the check runs wherever the file is present."""
    workbook = ROOT / _manifest()["source_workbook"]
    if not workbook.exists():
        pytest.skip(f"{workbook.name} is not in this checkout")
    assert _sha256(workbook) == _manifest()["source_sha256"]
