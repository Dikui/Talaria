---
status: accepted
---

# L0 底座只放纯契约与纯计算，有状态引擎和 I/O 放到上层

`talaria.foundation`（L0）原先混入了预警引擎、扫描引擎这类有状态的运行组件，以及 SQLite 守卫、契约导出、DSH 用量扫描等 I/O 代码，导致底座无法单独复用和测试，也让上层依赖方向变得混乱。现决定：L0 只保留无副作用的契约、算子和纯计算（如关键位引擎）；有状态引擎归入感知层（`perception/alerts`、`perception/scanner`），SQLite 守卫归入 `perception`，契约导出归入 `cli`，用量扫描归入 `workbench_api`。边界由 `tests/foundation/test_import_boundaries.py` 强制：L0 中任何模块导入 `sqlite3`、`duckdb`、`pyarrow`、`websockets`、`httpx`、`asyncio` 或任一上层包都会让测试失败。

## Considered Options

- **引擎留在 L0，图方便**：上层引用路径短，但 L0 会带上 asyncio、数据库等依赖，纯计算无法脱离运行时测试，层间依赖也会逐步倒挂。否决。
- **只靠文档约定，不加测试**：此前就是这样，结果约定被逐渐打破。否决。

## Consequences

- 新增引擎时先判断是否有状态或 I/O；有就放到上层，L0 只提供它用到的纯函数和契约类型。
- 关键位引擎是纯计算，因此留在 L0，这不是例外。
- 如确需在 L0 使用新的标准库或第三方依赖，先确认它没有 I/O 副作用，再调整导入边界测试中的禁止列表。
