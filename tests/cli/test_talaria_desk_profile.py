"""talaria-desk Profile 的权限边界：静态补丁 + 真机 dump-config 快照。

dump-config 只能证明工具/沙箱/审批配置；单调守卫由运行时 ``ctx.tools.guard``
注册，不会出现在 dump 中，行为测试见 dsh/packages/talaria-dsh-plugin/test/guard.test.mjs。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.medium

REPO = Path(__file__).resolve().parents[2]
PATCH = REPO / "dsh" / "profiles" / "talaria-desk.patch.yml"
PLUGIN = REPO / "dsh" / "packages" / "talaria-dsh-plugin"
SNAPSHOT = REPO / "tests" / "fixtures" / "dsh" / "talaria-desk.dump.snapshot.txt"
BLOCK_IDS = (
    "sandbox-policy",
    "approval",
    "permission",
    "agent-preset-registry",
    "preset-talaria-desk",
    "talaria-desk-plugin",
)


def test_patch_removes_execution_tools_and_keeps_read_only() -> None:
    text = PATCH.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "danger-full-access" not in body
    assert "mode: read-only" in text
    assert "policy: ask" in text
    assert "default: talaria-desk" in text
    for forbidden in (
        "@deepseek-ai/dsh-tool-bash",
        "@deepseek-ai/dsh-terminal",
        "@deepseek-ai/dsh-tool-write",
    ):
        assert forbidden not in text
    assert "write: false" in text
    assert "edit: false" in text
    assert "applyPatch: false" in text


def test_plugin_bundle_patch_is_the_canonical_patch() -> None:
    manifest = json.loads((PLUGIN / "package.json").read_text(encoding="utf-8"))
    assert manifest["dsh"]["bundle"]["patch"] == "./talaria-desk.patch.yml"
    assert (PLUGIN / "talaria-desk.patch.yml").read_text(
        encoding="utf-8"
    ) == PATCH.read_text(encoding="utf-8")


def _blocks(text: str) -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("== ") and line.endswith(" =="):
            current = line[3:-3]
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {
        key: "\n".join(lines).strip() + "\n" for key, lines in blocks.items()
    }


def _dump_blocks(text: str) -> dict[str, str]:
    lines = text.splitlines()
    blocks: dict[str, str] = {}
    for ident in BLOCK_IDS:
        start = next(
            (i for i, line in enumerate(lines) if line.startswith(f"- id: {ident}")),
            None,
        )
        if start is None:
            continue
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].startswith("- id: "):
                end = i
                break
        cleaned = [
            line
            for line in lines[start:end]
            if not line.lstrip().startswith("#")
        ]
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        blocks[ident] = "\n".join(cleaned).strip() + "\n"
    return blocks


@pytest.mark.skipif(shutil.which("dsh") is None, reason="本机未安装 dsh")
def test_real_dump_config_matches_permission_snapshot() -> None:
    profile_dir = (
        Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
        / "profiles"
        / "talaria-desk"
    )
    installed = profile_dir / "node_modules" / "@talaria" / "dsh-plugin" / "package.json"
    if not installed.is_file():
        pytest.skip("talaria-desk Profile 尚未安装 @talaria/dsh-plugin")

    completed = subprocess.run(
        ["dsh", "--profile", "talaria-desk", "--dump-config"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    dump = completed.stdout
    assert dump.count("- id: preset-talaria-desk") == 1
    assert dump.count("- id: talaria-desk-plugin") == 1
    expected = _blocks(SNAPSHOT.read_text(encoding="utf-8"))
    actual = _dump_blocks(dump)
    assert set(actual) == set(BLOCK_IDS)
    for ident in BLOCK_IDS:
        assert actual[ident] == expected[ident], ident
