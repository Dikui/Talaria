"""通过常驻服务或排他锁导入逐笔，并统计冷库容量。"""

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import httpx

from talaria.foundation.errors import ColdStoreError, ServiceLockError
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.cold_publisher import ColdPublisher
from talaria.perception.cold_store import ColdStore, tick_capacity
from talaria.service_endpoint import DATA_DIR_HEADER, data_dir_id, read_service_connection
from talaria.service_lock import ServiceLock


def _through_service(root: Path, instrument: str, day: date) -> int:
    connection = read_service_connection(root)
    if connection is None:
        raise ValueError("数据目录已锁定，未找到可接收补数请求的常驻服务")
    with httpx.Client(trust_env=False, timeout=600) as client:
        response = client.post(
            f"{connection.base_url}/api/trades/backfill",
            params={"instrument": instrument, "day": day.isoformat()},
            headers={"Authorization": f"Bearer {connection.token}"},
        )
    if response.is_error:
        raise ValueError(f"服务返回 {response.status_code}：{response.text}")
    if response.headers.get(DATA_DIR_HEADER) != data_dir_id(root):
        raise ValueError("补数响应的数据目录不匹配")
    inserted = response.json()
    if type(inserted) is not int or inserted < 0:
        raise ValueError("补数响应缺少有效的成交笔数")
    return inserted


def _backfill(args: argparse.Namespace) -> int:
    root = Path(args.data_dir).expanduser().resolve()
    lock = ServiceLock(root, role="trade-backfill")
    try:
        try:
            lock.acquire()
        except ServiceLockError:
            inserted = _through_service(root, args.instrument, args.day)
        else:
            spec = get_market_pack(DEFAULT_PACK).instrument(args.instrument)
            if spec is None:
                raise ValueError(f"未知标的：{args.instrument}")
            store = ColdStore(root)
            try:
                async def run() -> int:
                    publisher = ColdPublisher(
                        store, instruments=[spec], connector=None, policy=BackfillPolicy(),
                        now_ms=lambda: time.time_ns() // 1_000_000,
                    )
                    try:
                        return await publisher.backfill_trades(spec, args.day)
                    finally:
                        await publisher.close()

                inserted = asyncio.run(run())
            finally:
                store.close()
    except Exception as exc:
        print(f"逐笔历史补数失败：{exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()
    print(f"{args.day} {args.instrument} 新增 {inserted} 笔")
    return 0


def _capacity(args: argparse.Namespace) -> int:
    root = Path(args.data_dir).expanduser().resolve()
    day = None if args.day is None else args.day.isoformat()
    try:
        totals = tick_capacity(root, day)
    except (ColdStoreError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"读取冷库容量失败：{exc}", file=sys.stderr)
        return 1
    print("day\tvenue\tsymbol\tdataset\trows\tparquet_bytes")
    for row in totals:
        print("\t".join(map(str, row)))
    return 0


def add_tick_commands(subparsers) -> None:
    backfill = subparsers.add_parser("trade-backfill", help="补齐 Bybit 公开逐笔日归档")
    backfill.add_argument("instrument", help="标的 ID（如 BTC-USDT-PERP）")
    backfill.add_argument("day", type=date.fromisoformat, help="已结束的 UTC 日（YYYY-MM-DD）")
    backfill.set_defaults(func=_backfill)

    capacity = subparsers.add_parser("cold-size", help="按日统计清单中的逐笔和买一卖一 Parquet 体积")
    capacity.add_argument("--day", type=date.fromisoformat, help="指定 UTC 日（YYYY-MM-DD）")
    capacity.set_defaults(func=_capacity)

    for command in (backfill, capacity):
        command.add_argument(
            "--data-dir", default=os.environ.get("TALARIA_DATA_DIR") or ".talaria",
            help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）",
        )
