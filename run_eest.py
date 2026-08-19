#!/usr/bin/env python3
"""Replay EEST stateful-engine benchmarks against a local Besu snapshot.

Adapted from https://github.com/ahamlat/stateful-bench-replay for benchmarkoor
EEST fixtures (blockchain_test_stateful_engine).

Per test:
  reset overlay -> start Besu -> setup RPC lines (pre_run + anchor FCU) ->
  benchmark RPC lines -> stop Besu

Usage:
  ./runEestBenchmark.sh --snapshot /data/besu --fixtures /data/fixtures \\
      --filter '*ether_transfers*nonexistent*100M*'
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import jwt
import requests
import yaml

from eest_discovery import EestTestCase, discover_eest_tests

OVERLAY_SCRIPT = Path(__file__).resolve().parent / "scripts" / "overlay.sh"
DOCKER = ["sudo", "-n", "docker"]

_NEWPAYLOAD_OK = {"VALID"}
_FCU_OK = {"VALID"}

_JWT_FALLBACK_SOURCES = (
    Path("/data/jwt.hex"),
    Path.home() / ".besu" / "jwt.hex",
)


@dataclasses.dataclass
class BesuConfig:
    image: str
    container_name: str
    data_snapshot_dir: Path
    overlay_dir: Path
    jwt_secret_path: Path
    engine_url: str
    extra_args: list[str]
    extra_mounts: list[str]
    startup_timeout_s: int
    container_data_path: str
    entrypoint: str | None


@dataclasses.dataclass
class InputConfig:
    fixtures_dir: Path


@dataclasses.dataclass
class TestsConfig:
    filter: str
    order: str


@dataclasses.dataclass
class RunConfig:
    reset_overlay: bool
    log_dir: Path
    request_timeout_s: int
    fail_fast: bool
    stop_container_on_exit: bool


@dataclasses.dataclass
class Config:
    besu: BesuConfig
    input: InputConfig
    tests: TestsConfig
    run: RunConfig


def _abs_path(p: str | os.PathLike) -> Path:
    return Path(p).expanduser().resolve()


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    b, i, t, r = raw["besu"], raw["input"], raw["tests"], raw["run"]
    return Config(
        besu=BesuConfig(
            image=b["image"],
            container_name=b.get("container_name", "besu-eest-bench"),
            data_snapshot_dir=_abs_path(b["data_snapshot_dir"]),
            overlay_dir=_abs_path(b["overlay_dir"]),
            jwt_secret_path=_abs_path(b["jwt_secret_path"]),
            engine_url=b["engine_url"].rstrip("/"),
            extra_args=list(b.get("extra_args") or []),
            extra_mounts=list(b.get("extra_mounts") or []),
            startup_timeout_s=int(b.get("startup_timeout_s", 180)),
            container_data_path=str(b.get("container_data_path", "/opt/besu/data")),
            entrypoint=(str(b["entrypoint"]) if b.get("entrypoint") else None),
        ),
        input=InputConfig(
            fixtures_dir=_abs_path(i["fixtures_dir"]),
        ),
        tests=TestsConfig(
            filter=str(t.get("filter", "*")),
            order=str(t.get("order", "alphabetical")),
        ),
        run=RunConfig(
            reset_overlay=bool(r.get("reset_overlay", True)),
            log_dir=_abs_path(r.get("log_dir", "./runs")),
            request_timeout_s=int(r.get("request_timeout_s", 300)),
            fail_fast=bool(r.get("fail_fast", False)),
            stop_container_on_exit=bool(r.get("stop_container_on_exit", True)),
        ),
    )


class SweepLog:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.failures_path = root / "failures.jsonl"
        self.summary_path = root / "summary.json"
        self.events_path = root / "events.log"
        self._failures = self.failures_path.open("a", buffering=1)
        self._events = self.events_path.open("a", buffering=1)
        self.counters: dict[str, dict[str, int]] = {}

    def event(self, msg: str) -> None:
        ts = dt.datetime.now().isoformat(timespec="seconds")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        self._events.write(line + "\n")

    def _bucket(self, name: str) -> dict[str, int]:
        return self.counters.setdefault(name, {"ok": 0, "fail": 0, "total": 0})

    def record_ok(self, source: str) -> None:
        b = self._bucket(source)
        b["ok"] += 1
        b["total"] += 1

    def record_fail(self, source: str, line_no: int, kind: str, detail: dict) -> None:
        b = self._bucket(source)
        b["fail"] += 1
        b["total"] += 1
        rec = {
            "ts": dt.datetime.now().isoformat(timespec="milliseconds"),
            "source": source,
            "line": line_no,
            "kind": kind,
            **detail,
        }
        self._failures.write(json.dumps(rec) + "\n")

    def flush_summary(self, extra: dict | None = None) -> None:
        summary = {
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
            "files": self.counters,
            "totals": {
                "ok": sum(b["ok"] for b in self.counters.values()),
                "fail": sum(b["fail"] for b in self.counters.values()),
                "total": sum(b["total"] for b in self.counters.values()),
            },
        }
        if extra:
            summary.update(extra)
        self.summary_path.write_text(json.dumps(summary, indent=2))

    def close(self) -> None:
        try:
            self._failures.close()
        finally:
            self._events.close()


def _run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    if capture:
        return subprocess.run(cmd, check=check, text=True, capture_output=True)
    return subprocess.run(cmd, check=check)


def _container_exists(name: str) -> bool:
    res = _run(DOCKER + ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"], capture=True)
    return name in res.stdout.split()


def _container_running(name: str) -> bool:
    res = _run(DOCKER + ["ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"], capture=True)
    return name in res.stdout.split()


def _dump_container_logs(name: str, log: SweepLog, tail: int = 200) -> None:
    if not _container_exists(name):
        log.event(f"container {name} no longer exists; cannot dump logs")
        return
    res = _run(DOCKER + ["logs", "--tail", str(tail), name], check=False, capture=True)
    out = (res.stdout or "") + (res.stderr or "")
    log.event(f"--- last {tail} lines of docker logs {name} ---")
    for line in out.splitlines():
        log.event(f"  | {line}")
    log.event("--- end of container logs ---")


def stop_container(name: str) -> None:
    if not _container_exists(name):
        return
    _run(DOCKER + ["rm", "-f", name], check=False, capture=True)


def save_container_logs(name: str, dest: Path, log: SweepLog) -> None:
    if not _container_exists(name):
        return
    res = _run(DOCKER + ["logs", name], check=False, capture=True)
    out = (res.stdout or "") + (res.stderr or "")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out)
    log.event(f"saved {len(out.splitlines())} log lines to {dest}")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^\w\-.]+", "-", name)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:60] or "test"


def _besu_log_filename(idx: int, name: str, failed: bool = False) -> str:
    suffix = "-FAIL" if failed else ""
    return f"besu-{idx:04d}-{_slugify(name)}{suffix}.log"


def start_besu(cfg: BesuConfig, log: SweepLog) -> None:
    stop_container(cfg.container_name)
    merged = cfg.overlay_dir / "test" / "merged"
    docker_cmd: list[str] = list(DOCKER) + [
        "run", "-d",
        "--name", cfg.container_name,
        "--network", "host",
        "--security-opt", "seccomp=unconfined",
        "-v", f"{merged}:{cfg.container_data_path}",
    ]
    if cfg.entrypoint:
        docker_cmd += ["--entrypoint", cfg.entrypoint]
    for spec in cfg.extra_mounts:
        docker_cmd += ["-v", spec]
    docker_cmd.append(cfg.image)
    docker_cmd.append(f"--data-path={cfg.container_data_path}")
    docker_cmd += cfg.extra_args
    log.event("docker run: " + " ".join(shlex.quote(a) for a in docker_cmd))
    res = _run(docker_cmd, capture=True, check=False)
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        log.event(f"docker run FAILED ({res.returncode}): {err}")
        raise RuntimeError(f"docker run exited {res.returncode}: {err}")


def _overlay(action: str, cfg: BesuConfig, log: SweepLog) -> None:
    cmd = ["sudo", "-n", str(OVERLAY_SCRIPT), action]
    if action in ("init", "mount-all", "reset-all", "reset-test"):
        cmd += [str(cfg.data_snapshot_dir), str(cfg.overlay_dir)]
    else:
        cmd += [str(cfg.overlay_dir)]
    log.event(f"overlay {action}: " + " ".join(shlex.quote(a) for a in cmd))
    _run(cmd)


def overlay_reset_all(cfg: BesuConfig, log: SweepLog) -> None:
    _overlay("reset-all", cfg, log)


def per_test_reset(cfg: BesuConfig, log: SweepLog) -> None:
    overlay_reset_all(cfg, log)


def ensure_jwt_secret(path: Path, log: SweepLog) -> None:
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    for src in _JWT_FALLBACK_SOURCES:
        if src.is_file() and src.resolve() != path.resolve():
            shutil.copy2(src, path)
            log.event(f"jwt secret missing at {path}; copied from {src}")
            return
    import secrets as _secrets

    path.write_text(_secrets.token_hex(32))
    try:
        path.chmod(0o644)
    except OSError:
        pass
    log.event(f"jwt secret missing at {path}; generated a fresh one")


def load_jwt_secret(path: Path) -> bytes:
    raw = path.read_text().strip()
    if raw.startswith("0x"):
        raw = raw[2:]
    return bytes.fromhex(raw)


def make_jwt(secret: bytes) -> str:
    return jwt.encode({"iat": int(time.time())}, secret, algorithm="HS256")


def wait_for_engine(cfg: BesuConfig, secret: bytes, log: SweepLog) -> None:
    deadline = time.monotonic() + cfg.startup_timeout_s
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "engine_exchangeCapabilities", "params": [[]]}
    )
    log.event(f"waiting for Engine API at {cfg.engine_url} (timeout {cfg.startup_timeout_s}s)")
    last_err: str | None = None
    while time.monotonic() < deadline:
        if not _container_running(cfg.container_name):
            _dump_container_logs(cfg.container_name, log)
            raise RuntimeError(f"Besu container {cfg.container_name} exited during startup")
        try:
            r = requests.post(
                cfg.engine_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {make_jwt(secret)}",
                },
                timeout=5,
            )
            if r.status_code == 200 and "result" in r.json():
                log.event("Engine API is up")
                return
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except (requests.RequestException, ValueError) as exc:
            last_err = repr(exc)
        time.sleep(2)
    _dump_container_logs(cfg.container_name, log)
    raise RuntimeError(
        f"Engine API not ready in {cfg.startup_timeout_s}s; last error: {last_err}"
    )


def _rpc_http_url(cfg: BesuConfig) -> str:
    port = "8545"
    for arg in cfg.extra_args:
        if arg.startswith("--rpc-http-port="):
            port = arg.split("=", 1)[1]
    return f"http://127.0.0.1:{port}"


def query_chain_head(cfg: BesuConfig) -> tuple[int, str] | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    try:
        r = requests.post(_rpc_http_url(cfg), json=payload, timeout=10)
        if r.status_code != 200:
            return None
        result = r.json().get("result")
        if not result:
            return None
        block_no = int(result, 16)
        payload2 = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "eth_getBlockByNumber",
            "params": [result, False],
        }
        r2 = requests.post(_rpc_http_url(cfg), json=payload2, timeout=10)
        block = r2.json().get("result") or {}
        return block_no, block.get("hash", "?")
    except (requests.RequestException, ValueError, TypeError):
        return None


def log_chain_head(cfg: BesuConfig, log: SweepLog, prefix: str) -> None:
    head = query_chain_head(cfg)
    if head is None:
        log.event(f"{prefix}: chain head unavailable")
    else:
        block_no, block_hash = head
        log.event(f"{prefix}: head = #{block_no} {block_hash}")


def _classify(method: str, body: dict) -> tuple[bool, str, dict]:
    if "error" in body:
        return False, "rpc_error", {"error": body["error"]}
    result = body.get("result")
    if not isinstance(result, dict):
        return False, "no_result", {"body": body}
    if method.startswith("engine_newPayload"):
        status = (result.get("status") or "").upper()
        if status in _NEWPAYLOAD_OK:
            return True, "", {}
        return False, "newpayload_not_valid", {"result": result}
    if method.startswith("engine_forkchoiceUpdated"):
        ps = result.get("payloadStatus") or {}
        status = (ps.get("status") or "").upper()
        if status in _FCU_OK:
            return True, "", {}
        return False, "fcu_not_valid", {"result": result}
    return True, "", {}


def post_engine_line(
    cfg: Config, secret: bytes, session: requests.Session, raw: str
) -> tuple[int, dict | None, str | None]:
    try:
        resp = session.post(
            cfg.besu.engine_url,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {make_jwt(secret)}",
            },
            timeout=cfg.run.request_timeout_s,
        )
    except requests.RequestException as exc:
        return -1, None, repr(exc)
    try:
        body = resp.json()
    except ValueError as exc:
        return resp.status_code, None, f"bad_json:{exc!r}:{resp.text[:200]}"
    return resp.status_code, body, None


def replay_lines(
    cfg: Config,
    secret: bytes,
    session: requests.Session,
    lines: list[str],
    label: str,
    log: SweepLog,
    phase: str | None = None,
) -> bool:
    prefix = f"replay [{phase}] " if phase else "replay "
    log.event(f"{prefix}{label} ({len(lines)} lines)")

    for line_no, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            method = json.loads(raw).get("method", "?")
        except json.JSONDecodeError:
            log.record_fail(label, line_no, "bad_json", {})
            if cfg.run.fail_fast:
                return False
            continue

        status, body, err = post_engine_line(cfg, secret, session, raw)
        if err is not None and body is None:
            log.record_fail(label, line_no, "http_error", {"method": method, "error": err})
            if cfg.run.fail_fast:
                return False
            continue
        if status != 200:
            log.record_fail(
                label,
                line_no,
                "http_status",
                {
                    "method": method,
                    "status": status,
                    "body": json.dumps(body) if body is not None else err,
                },
            )
            if cfg.run.fail_fast:
                return False
            continue

        ok, kind, detail = _classify(method, body or {})
        if ok:
            log.record_ok(label)
        else:
            log.record_fail(label, line_no, kind, {"method": method, **detail})
            if cfg.run.fail_fast:
                return False
    return True


def _parse_last_imported(log_path: Path) -> dict | None:
    if not log_path.is_file():
        return None
    pattern = re.compile(
        r"Imported #(\d+) \((0x[0-9a-fA-F]+)\).*?(\d+) tx.*?(\d+) Mgas/s.*?exec\s+([\d.]+)ms"
    )
    last = None
    for line in log_path.read_text().splitlines():
        m = pattern.search(line)
        if m:
            last = {
                "block": int(m.group(1)),
                "hash": m.group(2),
                "mgas_per_s": float(m.group(4)),
                "exec_ms": float(m.group(5)),
            }
    return last


def run_sweep(
    cfg: Config,
    tests: list[EestTestCase],
    dry_run: bool,
) -> int:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_root = cfg.run.log_dir / timestamp
    log = SweepLog(log_root)
    log.event(f"sweep start, log dir = {log_root}")
    log.event(f"matched {len(tests)} EEST tests")

    (log_root / "selected_tests.txt").write_text(
        "\n".join(t.name for t in tests) + ("\n" if tests else "")
    )

    if dry_run:
        log.event("dry-run: wrote selected_tests.txt and exiting")
        for t in tests:
            print(f"  {t.name}  (setup={len(t.setup_lines)}, test={len(t.test_lines)})")
        log.flush_summary({"dry_run": True, "selected": len(tests)})
        log.close()
        return 0

    if not cfg.besu.data_snapshot_dir.is_dir():
        raise FileNotFoundError(f"snapshot dir missing: {cfg.besu.data_snapshot_dir}")

    ensure_jwt_secret(cfg.besu.jwt_secret_path, log)
    for spec in cfg.besu.extra_mounts:
        host = spec.split(":", 1)[0]
        if not host or not Path(host).exists():
            raise FileNotFoundError(f"besu.extra_mounts host path missing: {host!r}")

    for probe in (
        DOCKER + ["version", "--format", "{{.Server.Version}}"],
        ["sudo", "-n", str(OVERLAY_SCRIPT), "--help"],
    ):
        try:
            _run(probe, capture=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(
                f"preflight failed: {' '.join(probe)} -> {stderr}\n"
                "Configure passwordless sudo for docker and scripts/overlay.sh."
            ) from None

    secret = load_jwt_secret(cfg.besu.jwt_secret_path)
    started_container = False
    sweep_ok = True
    metrics: list[dict] = []

    try:
        with requests.Session() as session:
            for idx, test in enumerate(tests, start=1):
                log.event(f"[{idx}/{len(tests)}] {test.name}")
                per_test_reset(cfg.besu, log)

                start_besu(cfg.besu, log)
                started_container = True
                wait_for_engine(cfg.besu, secret, log)
                log_chain_head(cfg.besu, log, f"[{idx}/{len(tests)}] head BEFORE setup")

                test_ok = replay_lines(
                    cfg, secret, session, test.setup_lines, test.name, log, phase="setup"
                )
                if test_ok:
                    log_chain_head(cfg.besu, log, f"[{idx}/{len(tests)}] head AFTER setup")
                    test_ok = replay_lines(
                        cfg, secret, session, test.test_lines, test.name, log, phase="testing"
                    )
                    if test_ok:
                        log_chain_head(cfg.besu, log, f"[{idx}/{len(tests)}] head AFTER test")

                log_path = log.root / _besu_log_filename(idx, test.name, failed=not test_ok)
                save_container_logs(cfg.besu.container_name, log_path, log)
                imported = _parse_last_imported(log_path)
                if imported:
                    log.event(
                        f"[{idx}/{len(tests)}] Imported #{imported['block']}: "
                        f"{imported['mgas_per_s']:.2f} Mgas/s, exec {imported['exec_ms']:.1f}ms"
                    )
                    metrics.append({"test": test.name, **imported})

                stop_container(cfg.besu.container_name)
                started_container = False

                if not test_ok:
                    sweep_ok = False
                    break

        log.event(f"sweep end: ok={sweep_ok}")
    finally:
        if metrics:
            (log_root / "metrics.json").write_text(json.dumps(metrics, indent=2))
        log.flush_summary(
            {
                "config": {
                    "image": cfg.besu.image,
                    "snapshot": str(cfg.besu.data_snapshot_dir),
                    "fixtures_dir": str(cfg.input.fixtures_dir),
                    "selected_tests": len(tests),
                },
                "fail_fast_tripped": not sweep_ok,
            }
        )
        if started_container and cfg.run.stop_container_on_exit:
            stop_container(cfg.besu.container_name)
        log.close()

    return 0 if sweep_ok else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Replay EEST stateful-engine benchmarks against a Besu snapshot."
    )
    p.add_argument("--config", "-c", default=None, help="YAML config (optional if CLI paths given)")
    p.add_argument("--snapshot", "-s", default=None, help="Besu datadir (extracted snapshot)")
    p.add_argument("--fixtures", "-F", default=None, help="EEST fixtures directory")
    p.add_argument("--filter", "-f", default=None, help="glob on test names")
    p.add_argument("--test", "-t", action="append", default=None, help="exact test name (repeatable)")
    p.add_argument("--limit", "-n", type=int, default=None, help="run at most N tests")
    p.add_argument("--dry-run", action="store_true", help="list selected tests, no Besu/docker")
    return p.parse_args(argv)


def _default_config() -> Config:
    """Minimal config when only CLI paths are provided."""
    tool_dir = Path(__file__).resolve().parent
    return Config(
        besu=BesuConfig(
            image="hyperledger/besu:latest",
            container_name="besu-eest-bench",
            data_snapshot_dir=Path("/data/besu"),
            overlay_dir=Path("/data/besu-overlay"),
            jwt_secret_path=Path("/tmp/jwtsecret"),
            engine_url="http://127.0.0.1:8551",
            extra_args=[],
            extra_mounts=[],
            startup_timeout_s=180,
            container_data_path="/opt/besu/data",
            entrypoint=None,
        ),
        input=InputConfig(fixtures_dir=Path("/data/fixtures")),
        tests=TestsConfig(filter="*", order="alphabetical"),
        run=RunConfig(
            reset_overlay=True,
            log_dir=tool_dir / "runs",
            request_timeout_s=300,
            fail_fast=False,
            stop_container_on_exit=True,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    example = Path(__file__).resolve().parent / "config.example.yaml"
    config_path = _abs_path(args.config) if args.config else None
    if config_path and config_path.is_file():
        cfg = load_config(config_path)
    elif (Path(__file__).resolve().parent / "config.yaml").is_file() and not args.snapshot:
        cfg = load_config(Path(__file__).resolve().parent / "config.yaml")
    else:
        cfg = _default_config()
        if example.is_file():
            print(f"note: using defaults; copy {example.name} to config.yaml for full Besu flags")

    if args.snapshot:
        cfg.besu.data_snapshot_dir = _abs_path(args.snapshot)
    if args.fixtures:
        cfg.input.fixtures_dir = _abs_path(args.fixtures)

    pattern = args.filter or cfg.tests.filter
    explicit = args.test

    if not cfg.input.fixtures_dir.is_dir():
        print(f"error: fixtures dir missing: {cfg.input.fixtures_dir}", file=sys.stderr)
        return 2
    if not args.dry_run and not cfg.besu.data_snapshot_dir.is_dir():
        print(f"error: snapshot dir missing: {cfg.besu.data_snapshot_dir}", file=sys.stderr)
        return 2

    tests = discover_eest_tests(
        cfg.input.fixtures_dir,
        pattern=pattern,
        explicit=explicit,
        limit=args.limit,
    )
    if not tests:
        print(f"error: no tests matched filter={pattern!r}", file=sys.stderr)
        return 2

    signal.signal(signal.SIGINT, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    return run_sweep(cfg, tests, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
