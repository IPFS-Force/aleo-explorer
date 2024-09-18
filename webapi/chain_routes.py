import math
from decimal import Decimal
from typing import Any

from starlette.requests import Request

from aleo_types import u64
from db import Database
from webapi.utils import CJSONResponse, public_cache_seconds
from webui.classes import UIAddress


async def get_summary(db: Database):
    network_speed = await db.get_network_speed()
    validators = await db.get_current_validator_count()
    participation_rate = await db.get_network_participation_rate()
    block = await db.get_latest_block()
    summary = {
        "latest_height": block.height,
        "latest_timestamp": block.header.metadata.timestamp,
        "proof_target": block.header.metadata.proof_target,
        "coinbase_target": block.header.metadata.coinbase_target,
        "network_speed": network_speed,
        "validators": validators,
        "participation_rate": participation_rate,
    }
    return summary

@public_cache_seconds(5)
async def recent_blocks_route(request: Request):
    db: Database = request.app.state.db
    recent_blocks = await db.get_recent_blocks_fast(10)
    return CJSONResponse(recent_blocks)

@public_cache_seconds(5)
async def index_update_route(request: Request):
    db: Database = request.app.state.db
    last_block = request.query_params.get("last_block")
    if last_block is None:
        return CJSONResponse({"error": "Missing last_block parameter"}, status_code=400)
    try:
        last_block = int(last_block)
    except ValueError:
        return CJSONResponse({"error": "Invalid last_block parameter"}, status_code=400)
    if last_block < 0:
        return CJSONResponse({"error": "Negative last_block parameter"}, status_code=400)
    summary = await get_summary(db)
    result: dict[str, Any] = {"summary": summary}
    latest_height = await db.get_latest_height()
    if latest_height is None:
        return CJSONResponse({"error": "Database error"}, status_code=500)
    block_count = latest_height - last_block
    if block_count < 0:
        return CJSONResponse({"summary": summary})
    if block_count > 10:
        block_count = 10
    recent_blocks = await db.get_recent_blocks_fast(block_count)
    result["recent_blocks"] = recent_blocks
    return CJSONResponse(result)

@public_cache_seconds(5)
async def block_route(request: Request):
    db: Database = request.app.state.db
    height = request.path_params["height"]
    try:
        height = int(height)
    except ValueError:
        return CJSONResponse({"error": "Invalid height"}, status_code=400)
    block = await db.get_block_by_height(height)
    if block is None:
        return CJSONResponse({"error": "Block not found"}, status_code=404)

    coinbase_reward = await db.get_block_coinbase_reward_by_height(height)
    validators, all_validators_raw = await db.get_validator_by_height(height)
    all_validators: list[str] = []
    for v in all_validators_raw:
        all_validators.append(v["address"])
    css: list[dict[str, Any]] = []
    target_sum = 0
    if coinbase_reward is not None:
        solutions = await db.get_solution_by_height(height, 0, 100)
        for solution in solutions:
            css.append({
                "address": solution["address"],
                "counter": solution["counter"],
                "target": solution["target"],
                "reward": solution["reward"],
                "solution_id": solution["solution_id"],
            })
            target_sum += solution["target"]
    result: dict[str, Any] = {
        "block": block,
        "coinbase_reward": coinbase_reward,
        "validators": validators,
        "all_validators": all_validators,
        "solutions": css,
        "total_supply": Decimal(await db.get_total_supply_at_height(height)),
    }
    result["resolved_addresses"] = \
        await UIAddress.resolve_recursive_detached(
            result, db,
            await UIAddress.resolve_recursive_detached(
                result["solutions"], db, {}
            )
        )

    return CJSONResponse(result)


@public_cache_seconds(5)
async def blocks_route(request: Request):
    db: Database = request.app.state.db
    try:
        page = request.query_params.get("p")
        if page is None:
            page = 1
        else:
            page = int(page)
    except:
        return CJSONResponse({"error": "Invalid page"}, status_code=400)
    total_blocks = await db.get_latest_height()
    if not total_blocks:
        return CJSONResponse({"error": "No blocks found"}, status_code=500)
    total_blocks += 1
    total_pages = math.ceil(total_blocks / 20)
    if page < 1 or page > total_pages:
        return CJSONResponse({"error": "Invalid page"}, status_code=400)
    start = total_blocks - 1 - 20 * (page - 1)
    blocks = await db.get_blocks_range_fast(start, start - 20)

    return CJSONResponse({"blocks": blocks, "total_blocks": total_blocks, "total_pages": total_pages})

@public_cache_seconds(5)
async def validators_route(request: Request):
    db: Database = request.app.state.db
    try:
        page = request.query_params.get("p")
        if page is None:
            page = 1
        else:
            page = int(page)
    except:
        return CJSONResponse({"error": "Invalid page"}, status_code=400)
    latest_height = await db.get_latest_height()
    if latest_height is None:
        return CJSONResponse({"error": "No blocks found"}, status_code=500)
    total_validators = await db.get_validator_count_at_height(latest_height)
    if not total_validators:
        return CJSONResponse({"error": "No validators found"}, status_code=500)
    total_pages = (total_validators // 50) + 1
    if page < 1 or page > total_pages:
        return CJSONResponse({"error": "Invalid page"}, status_code=400)
    start = 50 * (page - 1)
    validators_data = await db.get_validators_range_at_height(latest_height, start, start + 50)
    validators: list[dict[str, Any]] = []
    total_stake = 0
    for validator in validators_data:
        validators.append({
            "address": validator["address"],
            "stake": u64(validator["stake"]),
            "uptime": validator["uptime"],
            "commission": validator["commission"],
            "open": validator["is_open"],
        })
        total_stake += validator["stake"]

    result = {
        "validators": validators,
        "total_stake": total_stake,
        "page": page,
        "total_pages": total_pages,
    }
    result["resolved_addresses"] = await UIAddress.resolve_recursive_detached(result, db, {})
    return CJSONResponse(result)