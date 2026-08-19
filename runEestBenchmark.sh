#!/usr/bin/env bash
# runEestBenchmark.sh - wrapper around run_eest.py for EEST stateful benchmarks.
#
# Usage:
#   ./runEestBenchmark.sh --snapshot /data/besu --fixtures /data/fixtures \\
#       --filter '*ether_transfers*nonexistent*100M*'
#   ./runEestBenchmark.sh --dry-run -s /data/besu -F /data/fixtures -f '*100M*'
#   ./runEestBenchmark.sh -c config.yaml --limit 1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/venv}"
CONFIG="${CONFIG:-config.yaml}"

if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
    echo "runEestBenchmark.sh: bootstrapping venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
    "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
fi

forward_config=true
has_snapshot=false
has_fixtures=false
for arg in "$@"; do
    case "$arg" in
        -c|--config|-c=*|--config=*)
            forward_config=true
            ;;
        -s|--snapshot|-s=*|--snapshot=*)
            has_snapshot=true
            forward_config=false
            ;;
        -F|--fixtures|-F=*|--fixtures=*)
            has_fixtures=true
            forward_config=false
            ;;
    esac
done

if $forward_config && [[ -f "$CONFIG" ]] && ! $has_snapshot && ! $has_fixtures; then
    set -- --config "$CONFIG" "$@"
fi

exec "$VENV_DIR/bin/python3" "$SCRIPT_DIR/run_eest.py" "$@"
