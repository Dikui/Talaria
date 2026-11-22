"""交易方案配置和带版本的插件注册表。"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, JsonValue, StrictInt, StringConstraints, model_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from talaria.account.parameters import ParameterSchema
from talaria.account.risk_config import AccountRiskConfig
from talaria.foundation.types import ContractModel

INTERFACE_VERSION = 2
ACCOUNT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
type Mode = Literal["backtest", "paper"]
type AccountId = Annotated[str, StringConstraints(pattern=ACCOUNT_ID_PATTERN)]
type NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type PluginParams = Mapping[str, JsonValue]


class SchemeError(ValueError):
    """交易方案无法装配。"""


class Slot(StrEnum):
    DATA = "data"
    STRATEGY = "strategy"
    SIZING = "sizing"
    RISK = "risk"
    FILL = "fill"
    TOOLS = "tools"


class PluginSelection(ContractModel):
    plugin: NonEmpty
    params: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def reference(self) -> str:
        return self.plugin


class PythonStrategySelection(ContractModel):
    python: NonEmpty
    params: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def reference(self) -> str:
        return f"{self.python}:plugin"


type NamedSelection = NonEmpty | PluginSelection
type Selection = PluginSelection | PythonStrategySelection


class SchemeSlots(ContractModel):
    data: NamedSelection
    strategy: NamedSelection | PythonStrategySelection
    sizing: NamedSelection
    risk: NamedSelection
    fill: NamedSelection
    tools: tuple[NonEmpty, ...] = Field(default=(), max_length=0)

    def selection(self, slot: Slot) -> Selection:
        selection = getattr(self, slot.value)
        return PluginSelection(plugin=selection) if isinstance(selection, str) else selection


class TradingScheme(ContractModel):
    scheme: NonEmpty
    interface_version: Annotated[StrictInt, Field(ge=INTERFACE_VERSION, le=INTERFACE_VERSION)]
    sub_accounts: tuple[AccountId, ...] = Field(min_length=1)
    seed: StrictInt = 0
    slots: SchemeSlots
    account_risk: dict[AccountId, AccountRiskConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _distinct_sub_accounts(self) -> Self:
        if len(set(self.sub_accounts)) != len(self.sub_accounts):
            raise ValueError("sub_accounts 不能重复")
        if self.account_risk.keys() - set(self.sub_accounts):
            raise ValueError("account_risk 只能配置方案声明的子账户")
        return self


class _SchemeLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict:
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in keys:
                raise ConstructorError(
                    None, None, f"交易方案的键必须为字符串且不可重复：{key!r}", key_node.start_mark,
                )
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


def load_scheme(path: Path) -> TradingScheme:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_SchemeLoader)
        return TradingScheme.model_validate(data)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise SchemeError(f"{path}：{exc}") from exc


@dataclass(frozen=True, slots=True)
class PluginContext:
    data_dir: Path
    mode: Mode
    seed: int
    now: Callable[[], int]
    account_risk: Mapping[str, AccountRiskConfig] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SlotPlugin[T]:
    slot: Slot
    compatible_versions: tuple[int, ...]
    create: Callable[[PluginContext, PluginParams], T]
    parameters_schema: ParameterSchema | None = None


def python_reference(reference: str, scheme_dir: Path) -> tuple[Path, str]:
    filename, _, symbol = reference.rpartition(":")
    path = scheme_dir / filename
    path = path.parent.resolve() / path.name
    if path.suffix != ".py" or not symbol.isidentifier():
        raise SchemeError("Python 插件引用格式为 文件.py:导出名")
    return path, symbol


def check_plugin(plugin: object, slot: Slot, version: int) -> SlotPlugin:
    if not isinstance(plugin, SlotPlugin) or plugin.slot != slot:
        raise SchemeError(f"插件必须声明为 {slot} 插槽的 SlotPlugin")
    if version not in plugin.compatible_versions:
        raise SchemeError(f"插件兼容接口版本 {plugin.compatible_versions}，方案要求 {version}")
    return plugin


def load_python_module(path: Path, source: bytes) -> ModuleType:
    name = "_talaria_plugin_" + sha256(str(path).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SchemeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    except BaseException:
        del sys.modules[name]
        raise
    return module


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[tuple[Slot, str], SlotPlugin] = {}
        self._modules: dict[Path, ModuleType] = {}

    def register(self, name: str, plugin: SlotPlugin) -> None:
        key = (plugin.slot, name)
        if key in self._plugins:
            raise SchemeError(f"{plugin.slot} 插槽重复注册插件 {name}")
        self._plugins[key] = plugin

    def resolve(
        self, slot: Slot, reference: str, version: int, scheme_dir: Path, *, mode: Mode = "backtest",
    ) -> SlotPlugin:
        try:
            if ":" in reference:
                if slot == Slot.STRATEGY:
                    raise SchemeError("Python 策略须经账户宿主校验来源与快照")
                if mode == "paper":
                    raise SchemeError("模拟盘尚未支持从交易方案加载 Python 插件")
                plugin = self._load_file(reference, scheme_dir)
            else:
                plugin = self._plugins.get((slot, reference))
                if plugin is None:
                    raise SchemeError("插件未注册")
            return check_plugin(plugin, slot, version)
        except Exception as exc:
            raise SchemeError(f"{slot} 插槽插件 {reference}：{exc}") from exc

    def _load_file(self, reference: str, scheme_dir: Path) -> object:
        path, symbol = python_reference(reference, scheme_dir)
        if path not in self._modules:
            self._modules[path] = load_python_module(path, path.read_bytes())
        return getattr(self._modules[path], symbol)
