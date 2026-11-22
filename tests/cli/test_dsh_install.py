from __future__ import annotations

import io
import json
import plistlib
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from talaria.cli import dsh_host
from talaria.cli import main as cli

pytestmark = [pytest.mark.small, pytest.mark.spec]

START = b"# >>> talaria (added by Talaria; remove this block to uninstall)"
END = b"# <<< talaria"


@pytest.fixture
def plugin_repo(tmp_path, monkeypatch):
    repo = tmp_path / 'repo "with spaces"'
    plugin = repo / "dsh/packages/talaria-dsh-plugin"
    plugin.mkdir(parents=True)
    actual_repo = Path(__file__).resolve().parents[2]
    source = actual_repo / "dsh/packages/talaria-dsh-plugin"
    for filename in ("package.json", "index.js", "bridge.js", "guard.js", "cordis.patch.yml"):
        shutil.copyfile(source / filename, plugin / filename)
    # The injected build runner stands in for pnpm. Supply a built client artifact.
    (plugin / "client").mkdir()
    (plugin / "client/index.js").write_text("// built client")
    shutil.copytree(actual_repo / "dsh/profiles", repo / "dsh/profiles")
    shutil.copytree(actual_repo / "contracts/schemas/dsh-tools", repo / "contracts/schemas/dsh-tools")
    monkeypatch.setattr(cli, "repo_root", lambda: repo)
    return plugin


@pytest.fixture
def patch_install_env(tmp_path, monkeypatch):
    dsh_home = tmp_path / "dsh-home"
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    pnpm = tmp_path / "pnpm"
    pnpm.touch(mode=0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    return dsh_home, pnpm


@pytest.mark.parametrize("source", ["explicit", "environment", "path", "desktop"])
@pytest.mark.parametrize("profile", ["talaria-desk", "custom"])
def test_installs_profile_scoped_config_with_resolved_dsh(
    tmp_path, monkeypatch, plugin_repo, source, profile
):
    # platform/04: the runner reads the local bundle as pnpm would, without running DSH.
    home = tmp_path / "home"
    profile_dir = home / "dsh" / "profiles" / profile
    profile_dir.mkdir(parents=True)
    (profile_dir / "package.json").write_text("{}")
    profile_patch = profile_dir / "cordis.patch.yml"
    profile_patch.write_text("# trader settings\n")
    job = home / "Library/LaunchAgents/com.talaria.workbench.plist"
    job.parent.mkdir(parents=True)
    data_dir = tmp_path / 'service "data"'
    job.write_bytes(plistlib.dumps({
        "ProgramArguments": ["/bin/talaria", "up", "--data-dir", str(data_dir)],
    }))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("DSH_HOME", str(home / "dsh"))
    monkeypatch.delenv("TALARIA_DATA_DIR", raising=False)
    monkeypatch.delenv("TALARIA_DSH_BIN", raising=False)

    commands = {}
    for name in ("explicit", "environment", "path", "desktop", "pnpm"):
        executable = tmp_path / name / ("pnpm" if name == "pnpm" else "dsh")
        executable.parent.mkdir()
        executable.touch(mode=0o755)
        commands[name] = executable
    monkeypatch.setattr(dsh_host, "DESKTOP_DSH_BIN", commands["desktop"], raising=False)
    paths = [commands["pnpm"].parent]
    if source != "desktop":
        paths.append(commands["path"].parent)
    monkeypatch.setenv("PATH", ":".join(map(str, paths)))
    if source in {"explicit", "environment"}:
        monkeypatch.setenv("TALARIA_DSH_BIN", str(commands["environment"]))

    installs = []

    def run(command, **kwargs):
        if command[0] != str(commands["pnpm"]):
            assert command[:5] == [str(commands[source]), "plugin", "--profile", profile, "add"]
            artifact = Path(command[5].removeprefix("file:"))
            data = artifact.read_bytes()
            installs.append((artifact, data))
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                manifest = json.load(archive.extractfile("package/package.json"))
                patch = manifest["dsh"]["bundle"]["patch"]
                config = archive.extractfile(f"package/{Path(patch)}").read().decode()
                tools = {name for name in archive.getnames() if name.endswith(".tool.json")}
            for row in ("sandbox-policy", "approval", "permission", "agent-preset-registry"):
                assert (f"- id: {row}\n" in config) is (profile == "talaria-desk")
            credential = json.dumps(str(data_dir / "runtime/dsh-bridge.json"), ensure_ascii=False)
            assert f"credentialFile: {credential}" in config
            assert "desktop: true" not in config
            assert config.count("name: Talaria 分析副驾驶") == 1
            assert len(tools) == 7
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)

    args = ["dsh-install", "--dsh-profile", profile]
    if source == "explicit":
        args += ["--dsh-bin", str(commands["explicit"])]
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    assert installs[0] == installs[-1]
    assert profile_patch.read_text() == "# trader settings\n"


@pytest.mark.regression
@pytest.mark.parametrize(
    ("original", "suffix", "after_uninstall"),
    [
        (
            b"# user patch\r\n- id: user-plugin\r\n",
            b"# next user change\r\n",
            b"# user patch\r\n- id: user-plugin\r\n# next user change\r\n",
        ),
        (b"# DSH template\n[]\n", b"", b"# DSH template\n[]\n"),
        (
            b"# DSH template\n[]\n",
            b"- id: added-user-plugin\n",
            b"# DSH template\n- id: added-user-plugin\n",
        ),
        (b"- id: no-final-newline", b"", b"- id: no-final-newline"),
    ],
)
def test_desktop_installs_updates_and_removes_only_its_patch(
    tmp_path, monkeypatch, plugin_repo, patch_install_env, original, suffix, after_uninstall
):
    # platform/04: the fake runner accepts the build and rejects any DSH CLI invocation.
    dsh_home, pnpm = patch_install_env
    path = dsh_home / "profiles/desktop/cordis.patch.yml"
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    path.chmod(0o640)
    builds = []

    def run(command, **kwargs):
        assert command[0] == str(pnpm)
        assert command[-3:] == ["--filter", "@talaria/dsh-plugin", "build"]
        builds.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    data_dir = tmp_path / 'service "data"'
    args = ["dsh-install", "--dsh-bin", str(tmp_path / "missing-dsh"), "--data-dir", str(data_dir)]
    assert cli.main(args) == 0
    first = path.read_bytes()
    assert first.count(START) == first.count(END) == 1
    assert first.startswith(original.split(b"[]", 1)[0])
    if b"[]" in original:
        assert b"\n[]\n" not in first
    preset = (plugin_repo / "cordis.patch.yml").read_bytes().split(b"    - id: talaria-desk-plugin")[0]
    assert preset in first.replace(b"\r\n", b"\n")
    entry = json.dumps(str(plugin_repo / "index.js"))
    credential = json.dumps(str(data_dir / "runtime/dsh-bridge.json"))
    assert (
        "    - id: talaria-desk-plugin\n"
        f"      name: {entry}\n"
        "      config:\n"
        f"        credentialFile: {credential}\n"
        "        desktop: true\n"
    ).encode() in first.replace(b"\r\n", b"\n")
    backups = dsh_home / "profiles-backup"
    backup_files = sorted(backups.glob("desktop-*/cordis.patch.yml"))
    assert [backup.read_bytes() for backup in backup_files] == [original]
    assert cli.main(args) == 0
    assert path.read_bytes() == first
    assert sorted(backups.glob("desktop-*/cordis.patch.yml")) == backup_files

    previous = b"".join(
        line for line in first.splitlines(keepends=True) if line.strip() != b"desktop: true"
    ) + suffix
    path.write_bytes(previous)
    assert cli.main([*args, "--data-dir", str(tmp_path / "new service")]) == 0
    latest = path.read_bytes()
    assert latest.count(START) == latest.count(END) == 1
    assert b"new service/runtime/dsh-bridge.json" in latest
    assert b"        desktop: true" in latest
    assert latest.endswith(suffix)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640

    assert cli.main(["dsh-uninstall"]) == 0
    assert cli.main(["dsh-uninstall"]) == 0
    assert path.read_bytes() == after_uninstall
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert [backup.read_bytes() for backup in sorted(backups.glob("desktop-*/cordis.patch.yml"))] == [
        original, previous, latest,
    ]
    assert len(builds) == 3


@pytest.mark.regression
@pytest.mark.parametrize("rejected_command", ["--dump-config", "add"])
def test_electron_managed_profile_reuses_patch_after_cli_refusal(
    tmp_path, monkeypatch, plugin_repo, patch_install_env, rejected_command
):
    # platform/04: reject the initial dump-config or plugin add as the Electron CLI would.
    dsh_home, pnpm = patch_install_env
    path = dsh_home / "profiles/custom/cordis.patch.yml"
    path.parent.mkdir(parents=True)
    if rejected_command == "add":
        (path.parent / "package.json").write_text("{}")
    dsh = tmp_path / "dsh"
    dsh.touch(mode=0o755)
    refusals = []

    def run(command, **kwargs):
        if command[0] == str(pnpm):
            return subprocess.CompletedProcess(command, 0)
        assert command[0] == str(dsh)
        assert rejected_command in command
        refusals.append(command)
        message = 'profile "custom" is managed exclusively by the Electron application'
        return subprocess.CompletedProcess(command, 1, "", message)

    monkeypatch.setattr(subprocess, "run", run)
    args = ["dsh-install", "--dsh-profile", "custom", "--data-dir", str(tmp_path / "data")]
    assert cli.main([*args, "--dsh-bin", str(dsh)]) == 0
    content = path.read_bytes()
    assert content.count(START) == content.count(END) == 1
    assert f"name: {json.dumps(str(plugin_repo / 'index.js'))}".encode() in content
    assert cli.main([*args, "--dsh-bin", str(tmp_path / "unavailable")]) == 0
    assert path.read_bytes() == content
    assert len(refusals) == 1
    assert cli.main(["dsh-uninstall", "--dsh-profile", "custom"]) == 0
    assert path.read_bytes() == b"[]\n"
