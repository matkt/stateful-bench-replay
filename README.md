# EEST Bench Replay

Local runner for **benchmarkoor EEST stateful-engine** benchmarks against an
extracted Besu snapshot. Forked and simplified from
[ahamlat/stateful-bench-replay](https://github.com/ahamlat/stateful-bench-replay).

Unlike the original tool (gas-bump + funding prelude + setup/testing `.txt`
files), this runner reads **EEST JSON fixtures** directly:

- `blockchain_test_stateful_engine` format
- Shared `pre_run/<startBlockHash>.json`
- Per-fixture `setupEngineNewPayloads` + `engineNewPayloads`

## Prerequisites

- Linux host with **OverlayFS** (macOS Docker bind mounts cannot use overlay)
- Docker + passwordless `sudo` for `docker` and `scripts/overlay.sh`
- Extracted Besu datadir (jochemnet snapshot, block 24402727)
- EEST fixtures directory (benchmarkoor suite `0d93b5bf3b970403`)
- Genesis JSON + JWT secret (same network as the snapshot)

## Quick start

```bash
cd tools/eest-bench-replay
cp config.example.yaml config.yaml
# Edit config.yaml: snapshot path, fixtures path, genesis, jwt, besu image

chmod +x runEestBenchmark.sh scripts/overlay.sh

# List matching tests (no Docker)
./runEestBenchmark.sh \
  --snapshot /data/besu/jochemnet-24402727 \
  --fixtures /data/blockchain_tests_stateful_engine \
  --filter '*ether_transfers*nonexistent*100M*' \
  --dry-run

# Run one test
./runEestBenchmark.sh \
  -s /data/besu/jochemnet-24402727 \
  -F /data/blockchain_tests_stateful_engine \
  -f '*ether_transfers*nonexistent*100M*' \
  --limit 1
```

## CLI

| Flag | Description |
|------|-------------|
| `--snapshot`, `-s` | Path to extracted Besu datadir (required unless in config) |
| `--fixtures`, `-F` | EEST fixtures root (with optional `pre_run/`) |
| `--filter`, `-f` | Glob on test names (default `*`) |
| `--test`, `-t` | Exact test name, repeatable (overrides filter) |
| `--limit`, `-n` | Run at most N tests after filtering |
| `--dry-run` | Resolve tests and exit |
| `--config`, `-c` | YAML config file |

## Per-test flow

```
reset OverlayFS → start Besu → anchor FCU + pre_run + setup payloads →
benchmark engineNewPayloads → stop Besu → parse Imported #… Mgas/s
```

Each test starts from the **same snapshot state** (overlay wiped every time).

## Results

Runs write to `runs/<timestamp>/`:

```
selected_tests.txt   # resolved test list
events.log           # timeline
failures.jsonl       # non-VALID Engine API responses
summary.json         # counters
metrics.json         # Mgas/s + exec ms from Besu logs
besu-0001-*.log      # full docker logs per test
```

## One-time setup (jochemnet / Amsterdam / BAL)

For the benchmarkoor run `besu-bal-full` (suite `0d93b5bf3b970403`):

### 1. Snapshot (extract once, pass path to `--snapshot`)

```bash
curl -L -o snapshot.tar.zst \
  https://snapshots.ethpandaops.io/jochemnet/besu/24402727/snapshot.tar.zst
mkdir -p /data/besu/jochemnet-24402727
tar --use-compress-program=unzstd -xf snapshot.tar.zst -C /data/besu/jochemnet-24402727
```

### 2. Fixtures (extract once, pass path to `--fixtures`)

Official EEST payload bundle for jochemnet v1 Amsterdam stateful benchmarks:

```bash
curl -L -o eest-payloads.tar.gz \
  'https://github.com/ethpandaops/benchmarkoor-tests/releases/download/eest-payloads-jochemnet-v1-amsterdam-stateful-d9ad55b3-20260807-000744/eest-payloads-jochemnet-v1-amsterdam-stateful-geth.tar.gz'
mkdir -p /data/eest-fixtures
tar -xzf eest-payloads.tar.gz -C /data/eest-fixtures
```

The archive name says `geth` but the payloads are standard EEST engine fixtures
(`blockchain_test_stateful_engine` JSON + `pre_run/`). After extraction, point
`--fixtures` at the directory that contains those subdirs (often the tarball
root, or `…/blockchain_tests_stateful_engine` if nested — use `--dry-run` to
confirm tests are discovered).

### 3. Besu image + genesis + JWT

- Image with `engine_newPayloadV5` (Amsterdam), e.g. your local Besu build or
  `ethpandaops/besu:…`
- Genesis + JWT matching jochemnet (see `config.example.yaml`)

Example filter for the non-existent receiver regression:

```bash
./runEestBenchmark.sh \
  -s /data/besu/jochemnet-24402727 \
  -F /data/eest-fixtures \
  -f '*ether_transfers_onchain_receivers*nonexistent*100M*'
```

## Sudoers (one-time)

```bash
sudo tee /etc/sudoers.d/besu-eest-bench >/dev/null <<EOF
$USER ALL=(root) NOPASSWD: $(pwd)/scripts/overlay.sh
$USER ALL=(root) NOPASSWD: /usr/bin/docker
EOF
sudo chmod 440 /etc/sudoers.d/besu-eest-bench
```

## Démarrage rapide (FR)

Ce script rejoue les benchmarks EEST stateful contre un snapshot Besu local.
Vous passez le chemin du snapshot **déjà extrait** (pas de téléchargement)
et un filtre de tests.

```bash
./runEestBenchmark.sh \
  --snapshot /chemin/vers/datadir/besu \
  --fixtures /chemin/vers/blockchain_tests_stateful_engine \
  --filter '*ether_transfers*nonexistent*100M*'
```

## License

Apache-2.0 (consistent with Hyperledger Besu).
