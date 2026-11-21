from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from talaria.foundation.errors import ServiceLockError
from talaria.workbench_api.service_lock import (
    ROLE_SERVICE,
    ServiceLock,
    lock_path,
    probe_lock,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.small, pytest.mark.spec]

_HOLDER = """
import sys
from pathlib import Path

from talaria.workbench_api.service_lock import ServiceLock

lock = ServiceLock(Path(sys.argv[1]), role="service")
lock.acquire()
print("locked", flush=True)
sys.stdin.readline()
lock.release()
"""

_CONTENDER = """
import sys
from pathlib import Path

from talaria.workbench_api.service_lock import ServiceLock
from talaria.foundation.errors import ServiceLockError

try:
    ServiceLock(Path(sys.argv[1]), role="usage-scan").acquire()
except ServiceLockError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(3)
raise SystemExit(0)
"""


def test_lock_path_lives_in_runtime_dir(tmp_path):
    assert lock_path(tmp_path) == tmp_path.resolve() / "runtime" / "service.lock"


def test_acquire_writes_holder_metadata(tmp_path):
    lock = ServiceLock(tmp_path)

    holder = lock.acquire()

    path = lock_path(tmp_path)
    assert holder.pid == os.getpid()
    assert holder.role == ROLE_SERVICE
    assert holder.started_at
    assert lock.path == path
    assert lock.pid == os.getpid()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert str(tmp_path.resolve()) in path.read_text(encoding="utf-8")
    lock.release()


def test_second_acquisition_in_same_process_fails_with_holder_details(tmp_path):
    lock = ServiceLock(tmp_path, role="service")
    lock.acquire()
    try:
        with pytest.raises(ServiceLockError) as excinfo:
            ServiceLock(tmp_path, role="usage-scan").acquire()
    finally:
        lock.release()

    message = str(excinfo.value)
    assert str(tmp_path.resolve()) in message
    assert str(lock_path(tmp_path)) in message
    assert f"pid={os.getpid()}" in message
    assert "service" in message


def test_probe_lock_reports_current_holder(tmp_path):
    assert probe_lock(tmp_path) is None

    lock = ServiceLock(tmp_path)
    lock.acquire()
    try:
        holder = probe_lock(tmp_path)
    finally:
        lock.release()

    assert holder is not None
    assert holder.pid == os.getpid()
    assert holder.role == ROLE_SERVICE
    assert holder.started_at is not None
    assert probe_lock(tmp_path) is None


def test_release_allows_reacquisition_and_context_manager(tmp_path):
    with ServiceLock(tmp_path, role="usage-scan") as lock:
        assert lock.pid == os.getpid()
        assert probe_lock(tmp_path) is not None

    assert probe_lock(tmp_path) is None
    with ServiceLock(tmp_path, role="usage-scan") as lock:
        assert lock.role == "usage-scan"


def test_two_processes_contending_for_one_data_dir(tmp_path):
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(tmp_path)],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdin is not None
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"

        contender = subprocess.run(
            [sys.executable, "-c", _CONTENDER, str(tmp_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert contender.returncode == 3, contender.stderr
        assert str(tmp_path.resolve()) in contender.stderr
        assert f"pid={holder.pid}" in contender.stderr

        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
