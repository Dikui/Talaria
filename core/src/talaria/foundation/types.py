"""L0 基础类型：十进制字符串、时点精度与 UTC 毫秒时间戳。"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer, model_validator
from pydantic.json_schema import WithJsonSchema

#: 十进制字符串的正则；所有价格、数量、金额字段的 JSON Schema 都必须是这个形状。
DECIMAL_PATTERN = r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$"


def decimal_to_string(value: Decimal) -> str:
    """把 Decimal 转成普通十进制字符串（不使用科学计数法）。"""
    return format(value, "f")


#: 内部保持 Decimal，JSON 边界序列化为十进制字符串，禁止 number/integer。
DecimalStr = Annotated[
    Decimal,
    PlainSerializer(decimal_to_string, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": DECIMAL_PATTERN}, mode="validation"),
    WithJsonSchema({"type": "string", "pattern": DECIMAL_PATTERN}, mode="serialization"),
]


class ContractModel(BaseModel):
    """所有跨进程契约的基类：拒绝未知字段，JSON 使用 snake_case。"""

    model_config = ConfigDict(extra="forbid")


class PrecisionTier(StrEnum):
    """时点可取信的程度。"""

    SECOND_VERIFIED = "SECOND_VERIFIED"
    SESSION_BOUND = "SESSION_BOUND"
    DATE_CONSERVATIVE = "DATE_CONSERVATIVE"


class TemporalStamp(ContractModel):
    """五重时点；所有分析都必须能回答“当时是否已经知道”。"""

    ts_event: int
    ts_published: int | None = None
    ts_available: int
    precision: PrecisionTier
    ts_ingested: int
    ts_committed: int

    @model_validator(mode="after")
    def _check_ordering(self) -> TemporalStamp:
        if self.ts_available < self.ts_event:
            raise ValueError("ts_available 不得早于 ts_event")
        if self.ts_committed < self.ts_ingested:
            raise ValueError("ts_committed 不得早于 ts_ingested")
        return self
