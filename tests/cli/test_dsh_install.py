from __future__ import annotations

import io
import json
import plistlib
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from talaria.cli import dsh_host
from talaria.cli import main as cli

pytestmark = [pytest.mark.small, pytest.mark.spec]


@pytest.mark.parametrize("source", ["explicit", "environment", "path", "desktop"])
@pytest.mark.parametrize("profile", ["desktop", "talaria-desk", "custom"])
def test_installs_profile_scoped_config_with_resolved_dsh(tmp_path, monkeypatch, source, profile):
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
            assert ("desktop: true" in config) is (profile == "desktop")
            assert config.count("name: Talaria 分析副驾驶") == 1
            assert len(tools) == 7
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    # A built artifact is already present during this injected build.
    repo = tmp_path / "repo"
    plugin = repo / "dsh/packages/talaria-dsh-plugin"
    plugin.mkdir(parents=True)
    actual_repo = Path(__file__).resolve().parents[2]
    for filename in ("package.json", "index.js", "bridge.js", "guard.js"):
        shutil.copyfile(actual_repo / "dsh/packages/talaria-dsh-plugin" / filename, plugin / filename)
    for patch in (actual_repo / "dsh/packages/talaria-dsh-plugin").glob("*.patch.yml"):
        shutil.copyfile(patch, plugin / patch.name)
    (plugin / "client").mkdir()
    (plugin / "client/index.js").write_text("// built client")
    shutil.copytree(actual_repo / "dsh/profiles", repo / "dsh/profiles")
    shutil.copytree(actual_repo / "contracts/schemas/dsh-tools", repo / "contracts/schemas/dsh-tools")
    monkeypatch.setattr(cli, "repo_root", lambda: repo)

    args = ["dsh-install"]
    if profile != "desktop":
        args += ["--dsh-profile", profile]
    if source == "explicit":
        args += ["--dsh-bin", str(commands["explicit"])]
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    assert installs[0] == installs[-1]
    assert profile_patch.read_text() == "# trader settings\n"
