"""macOS 策略子进程的资源上限和进程用量。"""

import ctypes
from dataclasses import dataclass
from functools import cache
from math import ceil
import os
import resource

CPU_SECONDS = 60
MEMORY_BUDGET = 256 * 1024 * 1024
MAX_RSS_BYTES = 256 * 1024 * 1024
_PROC_PIDTASKINFO = 4


@cache
def _proc_pidinfo():
    proc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
    proc.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    proc.restype = ctypes.c_int
    return proc


@cache
def _timebase() -> tuple[int, int]:
    info = (ctypes.c_uint32 * 2)()  # mach_timebase_info_data_t
    query = ctypes.CDLL("/usr/lib/libSystem.B.dylib").mach_timebase_info
    query.argtypes = (ctypes.c_void_p,)
    query.restype = ctypes.c_int
    if query(info) != 0 or info[1] == 0:
        raise OSError("无法读取 macOS CPU 时间换算比例")
    return info[0], info[1]


@dataclass(frozen=True)
class TaskUsage:
    address_space: int
    resident: int
    cpu_ns: int


def task_usage(pid: int) -> TaskUsage:
    info = (ctypes.c_uint64 * 12)()  # proc_taskinfo
    size = ctypes.sizeof(info)
    if _proc_pidinfo()(pid, _PROC_PIDTASKINFO, 0, info, size) != size:
        raise OSError(ctypes.get_errno(), "无法读取策略子进程的资源用量")
    numerator, denominator = _timebase()
    return TaskUsage(info[0], info[1], (info[2] + info[3]) * numerator // denominator)


def limit_resources() -> None:
    # macOS 的虚拟空间包含 dyld 等保留映射。给策略固定的额外分配额度。
    baseline = task_usage(os.getpid())
    for limit, requested in (
        (resource.RLIMIT_AS, baseline.address_space + MEMORY_BUDGET),
        (resource.RLIMIT_CPU, ceil(baseline.cpu_ns / 1_000_000_000) + CPU_SECONDS),
        (resource.RLIMIT_CORE, 0),
    ):
        bound = min(requested, *resource.getrlimit(limit))
        resource.setrlimit(limit, (bound, bound))
