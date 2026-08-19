"""Convert EEST stateful-engine fixtures to Engine API JSON-RPC lines.

Port of benchmarkoor/pkg/eest/converter.go (ConvertStatefulFixture).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

SUPPORTED_STATEFUL_FORMAT = "blockchain_test_stateful_engine"
ZERO_HASH = "0x" + "0" * 64


@dataclass
class ConvertedTest:
    name: str
    setup_lines: list[str]
    test_lines: list[str]
    genesis_hash: str
    final_hash: str
    payload_count: int


def _build_execution_payload_json(ep: dict[str, Any], version: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "parentHash": ep["parentHash"],
        "feeRecipient": ep["feeRecipient"],
        "stateRoot": ep["stateRoot"],
        "receiptsRoot": ep["receiptsRoot"],
        "logsBloom": ep["logsBloom"],
        "prevRandao": ep["prevRandao"],
        "blockNumber": ep["blockNumber"],
        "gasLimit": ep["gasLimit"],
        "gasUsed": ep["gasUsed"],
        "timestamp": ep["timestamp"],
        "extraData": ep["extraData"],
        "baseFeePerGas": ep["baseFeePerGas"],
        "blockHash": ep["blockHash"],
        "transactions": ep["transactions"],
    }
    if version >= 2 and ep.get("withdrawals") is not None:
        result["withdrawals"] = ep["withdrawals"]
    if version >= 3:
        if ep.get("blobGasUsed"):
            result["blobGasUsed"] = ep["blobGasUsed"]
        if ep.get("excessBlobGas"):
            result["excessBlobGas"] = ep["excessBlobGas"]
    if version >= 5:
        if ep.get("blockAccessList"):
            result["blockAccessList"] = ep["blockAccessList"]
        if ep.get("slotNumber"):
            result["slotNumber"] = ep["slotNumber"]
    return result


def _parse_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a fixture payload entry to internal fields."""
    params = raw.get("params") or []
    if not params:
        raise ValueError("params array is empty")

    np_version = int(raw["newPayloadVersion"])
    fcu_version = int(raw["forkchoiceUpdatedVersion"])
    ep = params[0]
    if isinstance(ep, str):
        ep = json.loads(ep)

    blob_hashes: list[str] = []
    parent_beacon = ""
    execution_requests: list[str] = []
    if len(params) > 1 and params[1] is not None:
        blob_hashes = params[1]
    if len(params) > 2 and params[2] is not None:
        parent_beacon = params[2]
    if len(params) > 3 and params[3] is not None:
        execution_requests = params[3]

    return {
        "execution_payload": ep,
        "blob_versioned_hashes": blob_hashes,
        "parent_beacon_block_root": parent_beacon,
        "execution_requests": execution_requests,
        "new_payload_version": np_version,
        "forkchoice_updated_version": fcu_version,
    }


def _build_new_payload_call(payload: dict[str, Any], rpc_id: int) -> str:
    version = payload["new_payload_version"]
    method = f"engine_newPayloadV{version}"
    ep = payload["execution_payload"]
    exec_payload = _build_execution_payload_json(ep, version)

    if version == 1:
        params: list[Any] = [exec_payload]
    elif version == 2:
        params = [exec_payload]
    elif version == 3:
        params = [
            exec_payload,
            payload["blob_versioned_hashes"],
            payload["parent_beacon_block_root"],
        ]
    elif version in (4, 5):
        params = [
            exec_payload,
            payload["blob_versioned_hashes"],
            payload["parent_beacon_block_root"],
            payload["execution_requests"],
        ]
    else:
        raise ValueError(f"unsupported payload version: {version}")

    return json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id},
        separators=(",", ":"),
    )


def _build_forkchoice_updated_call_for_hash(
    block_hash: str, version: int, rpc_id: int
) -> str:
    method = f"engine_forkchoiceUpdatedV{version}"
    forkchoice_state = {
        "headBlockHash": block_hash,
        "safeBlockHash": ZERO_HASH,
        "finalizedBlockHash": ZERO_HASH,
    }
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": method,
            "params": [forkchoice_state, None],
            "id": rpc_id,
        },
        separators=(",", ":"),
    )


def _build_forkchoice_updated_call(payload: dict[str, Any], rpc_id: int) -> str:
    ep = payload["execution_payload"]
    return _build_forkchoice_updated_call_for_hash(
        ep["blockHash"], payload["forkchoice_updated_version"], rpc_id
    )


def _build_anchor_forkchoice_call(
    setup_payloads: list[dict[str, Any]], benchmark_payloads: list[dict[str, Any]]
) -> str:
    payloads = setup_payloads or benchmark_payloads
    if not payloads:
        raise ValueError("no payload to derive the anchor from")
    ep = payloads[0]["execution_payload"]
    anchor = ep.get("parentHash")
    if not anchor:
        raise ValueError("first payload has no parentHash")
    return _build_forkchoice_updated_call_for_hash(
        anchor, payloads[0]["forkchoice_updated_version"], 0
    )


def _convert_payload(payload: dict[str, Any], rpc_id: int) -> list[str]:
    return [
        _build_new_payload_call(payload, rpc_id),
        _build_forkchoice_updated_call(payload, rpc_id),
    ]


def _fixture_payloads(fixture: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw_list = fixture.get(key) or []
    return [_parse_payload(item) for item in raw_list]


def _pre_run_payload_count(pre_run: dict[str, Any] | None) -> int:
    if pre_run is None:
        return 0
    return len(pre_run.get("engineNewPayloads") or [])


def convert_stateful_fixture(
    name: str,
    fixture: dict[str, Any],
    pre_run: dict[str, Any] | None,
) -> ConvertedTest:
    """Convert a stateful-engine fixture to setup/test RPC line lists."""
    benchmark_raw = fixture.get("engineNewPayloads") or []
    if not benchmark_raw:
        raise ValueError("fixture has no benchmark payloads")

    setup_payloads: list[dict[str, Any]] = []
    if pre_run is not None:
        setup_payloads.extend(_fixture_payloads(pre_run, "engineNewPayloads"))
    setup_payloads.extend(_fixture_payloads(fixture, "setupEngineNewPayloads"))
    benchmark_payloads = [_parse_payload(p) for p in benchmark_raw]

    result = ConvertedTest(
        name=name,
        setup_lines=[],
        test_lines=[],
        genesis_hash=str(fixture.get("snapshotBlockHash") or ""),
        final_hash="",
        payload_count=len(setup_payloads) + len(benchmark_payloads),
    )

    anchor_line = _build_anchor_forkchoice_call(setup_payloads, benchmark_payloads)
    result.setup_lines.append(anchor_line)

    rpc_id = 1
    for payload in setup_payloads:
        result.setup_lines.extend(_convert_payload(payload, rpc_id))
        rpc_id += 1

    for payload in benchmark_payloads:
        result.test_lines.extend(_convert_payload(payload, rpc_id))
        rpc_id += 1
        result.final_hash = payload["execution_payload"]["blockHash"]

    return result


def is_stateful_fixture(fixture: dict[str, Any]) -> bool:
    info = fixture.get("_info") or {}
    return info.get("fixture-format") == SUPPORTED_STATEFUL_FORMAT


def stateful_pre_run_missing(fixture: dict[str, Any]) -> bool:
    start = fixture.get("startBlockHash") or ""
    snapshot = fixture.get("snapshotBlockHash") or ""
    return bool(start) and start != snapshot
