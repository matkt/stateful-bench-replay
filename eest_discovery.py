"""Discover and convert EEST stateful-engine fixtures."""
from __future__ import annotations

import fnmatch
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from eest_converter import (
    ConvertedTest,
    convert_stateful_fixture,
    is_stateful_fixture,
    stateful_pre_run_missing,
)

SUPPORTED_STATEFUL_FORMAT = "blockchain_test_stateful_engine"

PRE_RUN_DIR_NAMES = ("pre_run", "pre-runs", "pre_runs")
FIXTURES_DIR_NAMES = (
    "blockchain_tests_stateful_engine",
    "eest-payloads",
    "fixtures",
)
SKIP_FIXTURE_PREFIXES = ("pre_run", "pre-runs", "pre_runs", "pre_alloc", ".meta")


@dataclass
class FixturesLayout:
    """Resolved directories after normalizing a user-supplied fixtures path."""

    user_path: Path
    fixtures_search_dir: Path
    pre_run_dir: Path | None = None
    pre_run_request: Path | None = None
    pre_run_files: list[Path] = field(default_factory=list)

    @property
    def pre_run_count(self) -> int:
        return len(self.pre_run_files)


@dataclass
class EestTestCase:
    name: str
    setup_lines: list[str]
    test_lines: list[str]
    source_file: str
    start_block_hash: str = ""
    snapshot_block_hash: str = ""
    pre_run_matched: bool = False
    pre_run_payload_count: int = 0
    setup_payload_count: int = 0


def _norm_hash(value: str) -> str:
    return (value or "").lower()


def _has_stateful_fixtures(path: Path) -> bool:
    if not path.is_dir():
        return False
    for candidate in path.rglob("*.json"):
        rel = candidate.relative_to(path)
        if rel.parts and rel.parts[0] in SKIP_FIXTURE_PREFIXES:
            continue
        try:
            raw = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        for fixture in raw.values():
            if isinstance(fixture, dict) and is_stateful_fixture(fixture):
                return True
    return False


def _find_fixtures_search_dir(root: Path) -> Path:
    root = root.resolve()

    for name in FIXTURES_DIR_NAMES:
        if root.name == name and name == "eest-payloads":
            for client in ("geth", "besu", "reth", "nethermind"):
                nested = root / client
                if _has_stateful_fixtures(nested):
                    return nested

    if _has_stateful_fixtures(root):
        return root

    for name in FIXTURES_DIR_NAMES:
        candidate = root / name
        if candidate.is_dir():
            if name == "eest-payloads":
                for client in ("geth", "besu", "reth", "nethermind"):
                    nested = candidate / client
                    if _has_stateful_fixtures(nested):
                        return nested
                if _has_stateful_fixtures(candidate):
                    return candidate
            elif _has_stateful_fixtures(candidate):
                return candidate

    for candidate in sorted(root.rglob("blockchain_tests_stateful_engine")):
        if candidate.is_dir() and _has_stateful_fixtures(candidate):
            return candidate

    for candidate in sorted(root.rglob("*.json")):
        if candidate.parent.name in SKIP_FIXTURE_PREFIXES:
            continue
        try:
            raw = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and any(
            isinstance(v, dict) and is_stateful_fixture(v) for v in raw.values()
        ):
            return candidate.parent

    return root


def _collect_pre_run_json_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(directory.rglob("*.json")):
        rel = path.relative_to(directory)
        if rel.parts and rel.parts[0] in (".meta",):
            continue
        out.append(path)
    return out


def _find_pre_run_request(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    for name in ("pre-run.request", "pre_run.request", "pre-run.txt", "gas-bump.txt"):
        direct = directory / name
        if direct.is_file():
            return direct
        for path in sorted(directory.rglob(name)):
            if path.is_file():
                return path
    return None


def _find_pre_run_dir(root: Path, fixtures_search_dir: Path) -> tuple[Path | None, list[Path], Path | None]:
    search_roots = [root, fixtures_search_dir, fixtures_search_dir.parent, root.parent]
    seen: set[Path] = set()
    pre_run_request: Path | None = None

    for base in search_roots:
        base = base.resolve()
        if base in seen:
            continue
        seen.add(base)

        for name in PRE_RUN_DIR_NAMES:
            candidate = base / name
            if not candidate.is_dir():
                continue
            files = _collect_pre_run_json_files(candidate)
            if files:
                req = _find_pre_run_request(candidate)
                if req is not None:
                    pre_run_request = pre_run_request or req
                return candidate, files, pre_run_request

            for client in ("geth", "besu", "reth", "nethermind"):
                nested = candidate / client
                if not nested.is_dir():
                    continue
                files = _collect_pre_run_json_files(nested)
                req = _find_pre_run_request(nested)
                if req is not None:
                    pre_run_request = pre_run_request or req
                if files or req is not None:
                    return nested, files, pre_run_request or req

            bundle = candidate / "geth" / "pre_run_bundle"
            if bundle.is_dir():
                files = _collect_pre_run_json_files(bundle)
                req = _find_pre_run_request(bundle)
                if req is not None:
                    pre_run_request = pre_run_request or req
                if files or req is not None:
                    return bundle, files, pre_run_request or req

    return None, [], pre_run_request


def resolve_fixtures_layout(user_path: Path) -> FixturesLayout:
    """Resolve fixture JSON search dir and pre_run dir from a user-supplied path."""
    user_path = user_path.resolve()
    if not user_path.is_dir():
        raise FileNotFoundError(f"fixtures dir missing: {user_path}")

    fixtures_search_dir = _find_fixtures_search_dir(user_path)
    pre_run_dir, pre_run_files, pre_run_request = _find_pre_run_dir(user_path, fixtures_search_dir)

    return FixturesLayout(
        user_path=user_path,
        fixtures_search_dir=fixtures_search_dir,
        pre_run_dir=pre_run_dir,
        pre_run_request=pre_run_request,
        pre_run_files=pre_run_files,
    )


def load_pre_runs(layout: FixturesLayout) -> dict[str, dict]:
    """Load pre_run JSON files keyed by startBlockHash (case-insensitive)."""
    out: dict[str, dict] = {}
    for path in layout.pre_run_files:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warn: failed to parse pre_run {path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        key = _norm_hash(data.get("startBlockHash") or path.stem)
        out[key] = data
    return out


def _normalize_test_name(name: str) -> str:
    if name.startswith("tests/"):
        return name[len("tests/") :]
    return name


def _should_skip_fixture_path(rel: Path) -> bool:
    if not rel.parts:
        return False
    first = rel.parts[0]
    if first in SKIP_FIXTURE_PREFIXES:
        return True
    if first.startswith("."):
        return True
    return False


def _expected_setup_lines(
    setup_payload_count: int,
    pre_run_payload_count: int,
) -> int:
    payloads = pre_run_payload_count + setup_payload_count
    if payloads == 0:
        return 1  # anchor FCU only
    return 1 + payloads * 2


def discover_eest_tests(
    fixtures_dir: Path,
    pattern: str = "*",
    explicit: list[str] | None = None,
    limit: int | None = None,
    layout: FixturesLayout | None = None,
    strict_setup: bool = False,
) -> tuple[list[EestTestCase], FixturesLayout]:
    """Walk fixtures_dir, convert matching stateful fixtures."""
    if layout is None:
        layout = resolve_fixtures_layout(fixtures_dir)

    fixtures_search_dir = layout.fixtures_search_dir
    pre_runs = load_pre_runs(layout)
    tests: list[EestTestCase] = []

    for path in sorted(fixtures_search_dir.rglob("*.json")):
        rel = path.relative_to(fixtures_search_dir)
        if _should_skip_fixture_path(rel):
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
            pre_run = pre_runs.get(_norm_hash(start_hash))
            setup_payload_count = len(fixture.get("setupEngineNewPayloads") or [])
            pre_run_payload_count = len(pre_run.get("engineNewPayloads") or []) if pre_run else 0

            if pre_run is None and stateful_pre_run_missing(fixture):
                print(
                    f"warn: no pre_run for {test_name} "
                    f"(start={start_hash}, snapshot={fixture.get('snapshotBlockHash')}); "
                    f"looked under {layout.pre_run_dir or layout.user_path / 'pre_run'}",
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

            expected = _expected_setup_lines(setup_payload_count, pre_run_payload_count)
            if strict_setup and len(converted.setup_lines) < expected:
                print(
                    f"error: {test_name}: setup has {len(converted.setup_lines)} RPC lines "
                    f"but expected >= {expected} "
                    f"(pre_run payloads={pre_run_payload_count}, "
                    f"setupEngineNewPayloads={setup_payload_count}). "
                    f"Check pre_run dir: {layout.pre_run_dir}",
                    file=sys.stderr,
                )
                continue

            tests.append(
                EestTestCase(
                    name=test_name,
                    setup_lines=converted.setup_lines,
                    test_lines=converted.test_lines,
                    source_file=str(path),
                    start_block_hash=start_hash,
                    snapshot_block_hash=str(fixture.get("snapshotBlockHash") or ""),
                    pre_run_matched=pre_run is not None,
                    pre_run_payload_count=pre_run_payload_count,
                    setup_payload_count=setup_payload_count,
                )
            )

    tests.sort(key=lambda t: t.name)
    if limit is not None and limit >= 0:
        tests = tests[:limit]
    return tests, layout


def format_layout_report(layout: FixturesLayout, tests: list[EestTestCase]) -> str:
    lines = [
        f"fixtures user path:     {layout.user_path}",
        f"fixtures search dir:    {layout.fixtures_search_dir}",
        f"pre_run dir:            {layout.pre_run_dir or '(not found)'}",
        f"pre_run json files:     {layout.pre_run_count}",
    ]
    if layout.pre_run_request is not None:
        lines.append(f"pre_run request file:   {layout.pre_run_request}")
    lines.append(f"matched tests:          {len(tests)}")
    return "\n".join(lines)


def format_test_report(test: EestTestCase) -> str:
    expected = _expected_setup_lines(test.setup_payload_count, test.pre_run_payload_count)
    pre = "yes" if test.pre_run_matched else "no"
    return (
        f"  {test.name}\n"
        f"    setup_lines={len(test.setup_lines)} test_lines={len(test.test_lines)} "
        f"(expected setup>={expected})\n"
        f"    pre_run={pre} pre_run_payloads={test.pre_run_payload_count} "
        f"setupEngineNewPayloads={test.setup_payload_count}\n"
        f"    startBlockHash={test.start_block_hash}\n"
        f"    source={test.source_file}"
    )
