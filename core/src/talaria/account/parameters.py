"""交易方案参数的严格校验，支持 Pydantic 模型和组件参数 Schema。"""

from collections.abc import Mapping
from math import isfinite
import operator
import re

from pydantic import BaseModel

type ParameterSchema = type[BaseModel] | Mapping[str, object]

_TYPES = {
    "object": (dict,), "array": (list,), "string": (str,), "boolean": (bool,),
    "integer": (int,), "number": (int, float), "null": (type(None),),
}
_BOUNDS = {
    "minimum": operator.ge, "maximum": operator.le,
    "exclusiveMinimum": operator.gt, "exclusiveMaximum": operator.lt,
    "minLength": operator.ge, "maxLength": operator.le,
    "minItems": operator.ge, "maxItems": operator.le,
}
_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "pattern", "title", "description", "default", "examples",
    *_BOUNDS,
}


def validate_parameters(params: dict, schema: ParameterSchema) -> None:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        schema.model_validate(params, strict=True, extra="forbid")
    else:
        _validate(params, schema, "params")


def _validate(value: object, schema: object, path: str) -> None:
    if not isinstance(schema, Mapping):
        raise ValueError(f"{path} 的参数 Schema 必须是对象")
    unsupported = schema.keys() - _KEYWORDS
    if unsupported:
        raise ValueError(f"{path} 的参数 Schema 含未支持的关键字 {', '.join(sorted(unsupported))}")
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if any(not isinstance(name, str) or name not in _TYPES for name in names):
            raise ValueError(f"{path} 的参数 Schema 包含未知类型 {expected}")
        if not any(type(value) in _TYPES[name] for name in names):
            raise ValueError(f"{path} 必须为 {expected}")
    if type(value) is float and not isfinite(value):
        raise ValueError(f"{path} 必须为有限数值")
    if "enum" in schema and not any(_same_value(value, choice) for choice in schema["enum"]):
        raise ValueError(f"{path} 必须为枚举值之一")
    if "const" in schema and not _same_value(value, schema["const"]):
        raise ValueError(f"{path} 不等于 Schema 声明的常量")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                raise ValueError(f"{path}.{key} 为必填字段")
        for key, item in value.items():
            if key not in properties:
                raise ValueError(f"{path}.{key} 为未知字段")
            _validate(item, properties[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        raise ValueError(f"{path} 不符合 Schema 的 pattern")
    for keyword in schema.keys() & _BOUNDS.keys():
        if keyword.endswith(("Length", "Items")):
            if not isinstance(value, str if keyword.endswith("Length") else list):
                continue
            measured = len(value)
        elif type(value) in (int, float):
            measured = value
        else:
            continue
        if not _BOUNDS[keyword](measured, schema[keyword]):
            raise ValueError(f"{path} 不符合 Schema 的 {keyword}={schema[keyword]}")


def _same_value(value: object, choice: object) -> bool:
    if isinstance(value, list) and isinstance(choice, list):
        return len(value) == len(choice) and all(_same_value(a, b) for a, b in zip(value, choice))
    if isinstance(value, dict) and isinstance(choice, dict):
        return value.keys() == choice.keys() and all(_same_value(value[key], choice[key]) for key in value)
    return (
        type(value) is type(choice) or (type(value) in (int, float) and type(choice) in (int, float))
    ) and value == choice
