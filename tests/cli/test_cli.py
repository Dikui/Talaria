"""CLI 参数边界：必须 --solo、回环地址、端口与退出码。"""

from __future__ import annotations

import pytest

from talaria.cli.main import main, validate_loopback_host

pytestmark = pytest.mark.small


def test_up_without_solo_explains_m2(capsys) -> None:
    assert main(["up"]) == 2
    assert "DSH 协同模式将在 M2 提供，请使用 --solo" in capsys.readouterr().err


def test_up_rejects_non_loopback_host(capsys) -> None:
    assert main(["up", "--solo", "--host", "0.0.0.0"]) == 2
    assert "只允许绑定回环地址" in capsys.readouterr().err


def test_up_rejects_invalid_port(capsys) -> None:
    assert main(["up", "--solo", "--port", "0"]) == 2
    assert "端口必须在 1..65535" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert validate_loopback_host(host) == host


def test_contract_check_has_zero_drift(capsys) -> None:
    assert main(["contract-check"]) == 0
    assert "零漂移" in capsys.readouterr().out


def test_up_rejects_missing_replay_file(capsys, tmp_path) -> None:
    missing = tmp_path / "nope.jsonl"
    assert main(["up", "--solo", "--replay-events", str(missing)]) == 2
    assert "回放文件不存在" in capsys.readouterr().err


def test_up_rejects_replay_directory(capsys, tmp_path) -> None:
    assert main(["up", "--solo", "--replay-events", str(tmp_path)]) == 2
    assert "回放文件不存在" in capsys.readouterr().err


@pytest.mark.parametrize("speed", ["0", "-1", "-2.5"])
def test_up_rejects_non_positive_replay_speed(capsys, tmp_path, speed: str) -> None:
    replay = tmp_path / "demo.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    assert main(["up", "--solo", "--replay-events", str(replay), "--replay-speed", speed]) == 2
    assert "回放速度必须为正数" in capsys.readouterr().err
