"""Discover and convert EEST stateful-engine fixtures."""
from __future__ import annotations

import fnmatch
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from eest_converter import (
    ConvertedTest,
    convert_stateful_fixture,
    is_stateful_fixture,
    stateful_pre_run_missing,
)

SUPPORTED_STATEFUL_FORMAT = "blockchain_test_stateful_engine"


@dataclass
class EestTestCase:
    name: str
    setup_lines: list[str]
    test_lines: list[str]
    source_file: str


def load_pre_runs(fixtures_dir: Path) -> dict[str, dict]:
    """Load pre_run/*.json keyed by startBlockHash (filename stem)."""
    pre_run_dir = fixtures_dir / "pre_run"
    if not pre_run_dir.is_dir():
        return {}

    out: dict[str, dict] = {}
    for path in sorted(pre_run_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warn: failed to parse pre_run {path}: {exc}", file=sys.stderr)
            continue
        key = data.get("startBlockHash") or path.stem
        out[key] = data
    return out


def _normalize_test_name(name: str) -> str:
    if name.startswith("tests/"):
        return name[len("tests/") :]
    return name


def discover_eest_tests(
    fixtures_dir: Path,
    pattern: str = "*",
    explicit: list[str] | None = None,
    limit: int | None = None,
) -> list[EestTestCase]:
    """Walk fixtures_dir, convert matching stateful fixtures."""
    fixtures_dir = fixtures_dir.resolve()
    if not fixtures_dir.is_dir():
        raise FileNotFoundError(f"fixtures dir missing: {fixtures_dir}")

    pre_runs = load_pre_runs(fixtures_dir)
    tests: list[EestTestCase] = []

    for path in sorted(fixtures_dir.rglob("*.json")):
        rel = path.relative_to(fixtures_dir)
        if rel.parts and rel.parts[0] in ("pre_run", "pre_alloc"):
            continue

        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warn: skipping {path}: {exc}", file=sys.stderr)
            continue

        if not isinstance(raw, dict):
            continue

        for fixture_key, fixture in raw.items():
            if not isinstance(fixture, dict):
                continue
            if not is_stateful_fixture(fixture):
                continue

            test_name = _normalize_test_name(fixture_key)
            if explicit is not None:
                if test_name not in explicit and fixture_key not in explicit:
                    continue
            elif not fnmatch.fnmatch(test_name, pattern) and not fnmatch.fnmatch(
                fixture_key, pattern
            ):
                continue

            start_hash = fixture.get("startBlockHash") or ""
            pre_run = pre_runs.get(start_hash)
            if pre_run is None and stateful_pre_run_missing(fixture):
                print(
                    f"warn: no pre_run for {test_name} "
                    f"(start={start_hash}, snapshot={fixture.get('snapshotBlockHash')}); "
                    "replaying setup payloads only",
                    file=sys.stderr,
                )

            try:
                converted: ConvertedTest = convert_stateful_fixture(
                    test_name, fixture, pre_run
                )
            except ValueError as exc:
                print(f"warn: skipping {test_name}: {exc}", file=sys.stderr)
                continue

            if not converted.test_lines:
                print(f"warn: skipping {test_name}: no benchmark lines", file=sys.stderr)
                continue

            tests.append(
                EestTestCase(
                    name=test_name,
                    setup_lines=converted.setup_lines,
                    test_lines=converted.test_lines,
                    source_file=str(path),
                )
            )

    tests.sort(key=lambda t: t.name)
    if limit is not None and limit >= 0:
        tests = tests[:limit]
    return tests
