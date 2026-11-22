"""任务单 01 / architecture.md §3.1：L0 不依赖 I/O 库或业务运行层。"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

import talaria.foundation

FOUNDATION_ROOT = Path(talaria.foundation.__file__).parent
FORBIDDEN_IMPORTS = (
    "sqlite3",
    "duckdb",
    "pyarrow",
    "websockets",
    "httpx",
    "compression",
    "asyncio",
    "talaria.perception",
    "talaria.feed",
    "talaria.workbench_api",
    "talaria.packs",
    "talaria.cli",
    "talaria.sdk",
)


@pytest.mark.small
@pytest.mark.spec
@pytest.mark.parametrize(
    "path",
    sorted(FOUNDATION_ROOT.rglob("*.py")),
    ids=lambda path: path.relative_to(FOUNDATION_ROOT).as_posix(),
)
def test_foundation_imports_only_pure_dependencies(path: Path) -> None:
    relative = path.relative_to(FOUNDATION_ROOT).with_suffix("")
    parts = ("talaria", "foundation", *relative.parts)
    module = ".".join(parts[:-1] if path.stem == "__init__" else parts)
    package = module if path.stem == "__init__" else module.rpartition(".")[0]
    violations = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            source = resolve_name("." * node.level + (node.module or ""), package)
            imports = [source, *(f"{source}.{alias.name}" for alias in node.names)]
        else:
            continue
        for imported in imports:
            if any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for forbidden in FORBIDDEN_IMPORTS
            ):
                violations.append(f"{module}:{node.lineno} imports {imported}")
    assert not violations, "L0 导入边界违规：\n" + "\n".join(violations)
