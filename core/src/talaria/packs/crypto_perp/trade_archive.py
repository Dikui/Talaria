"""Bybit Linear 公开逐笔日归档。"""

import csv
import gzip
from collections.abc import Iterator
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import BinaryIO
from uuid import UUID

import httpx

from talaria.foundation.market import InstrumentSpec
from talaria.foundation.ticks import Trade

DAY_MS = 86_400_000
_REQUIRED = {"timestamp", "symbol", "side", "size", "price", "trdMatchID"}


def archive_url(symbol: str, day: date) -> str:
    if not symbol.isascii() or not symbol.isalnum():
        raise ValueError(f"非法 Bybit 标的代码：{symbol}")
    path = PurePosixPath("trading", symbol, f"{symbol}{day.isoformat()}.csv.gz")
    return f"https://public.bybit.com/{path}"


def download_bybit_archive(url: str, target: BinaryIO) -> None:
    """下载 gzip 原始字节，沿用 HTTPX 的环境代理。"""
    with httpx.stream("GET", url, headers={"Accept-Encoding": "identity"}, timeout=60) as response:
        response.raise_for_status()
        for chunk in response.iter_raw():
            target.write(chunk)


def archive_day_start(day: date) -> int:
    epoch = date(1970, 1, 1)
    return (day - epoch).days * DAY_MS


def check_archive_day(day: date, now_ms: int) -> None:
    if not 0 <= archive_day_start(day) <= now_ms - DAY_MS:
        raise ValueError("逐笔归档日期必须是已结束的 UTC 日")


def iter_archive_trades(
    source: BinaryIO, instrument: InstrumentSpec, day: date, received_ms: int,
) -> Iterator[Trade]:
    """读至 EOF 时 gzip 校验 CRC 和长度。交易时间按 ADR-0017 截至 UTC 毫秒。"""
    start = archive_day_start(day)
    with gzip.open(source, "rt", encoding="utf-8-sig", newline="") as text:
        rows = csv.DictReader(text, strict=True)
        fields = rows.fieldnames or []
        if not _REQUIRED.issubset(fields) or len(fields) != len(set(fields)):
            raise ValueError("Bybit 归档 CSV 列头缺失或重复")
        for values in rows:
            try:
                if None in values or None in values.values():
                    raise ValueError("行列数与列头不一致")
                if values["symbol"] != instrument.symbol:
                    raise ValueError("标的与归档请求不一致")
                side = values["side"]
                if side not in ("Buy", "Sell"):
                    raise ValueError("主动成交方向非法")
                seconds = Decimal(values["timestamp"])
                if not seconds.is_finite() or not start <= seconds * 1000 < start + DAY_MS:
                    raise ValueError("成交不在请求的 UTC 日内")
                ts = int(seconds * 1000)
                yield Trade(
                    instrument_id=instrument.instrument_id, trade_id=UUID(values["trdMatchID"]),
                    side=side, price=Decimal(values["price"]), size=Decimal(values["size"]),
                    ts_event=ts, ts_available=ts, ts_ingested=received_ms, ts_committed=received_ms,
                )
            except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Bybit 归档 CSV 第 {rows.line_num} 行无效：{exc}") from exc
