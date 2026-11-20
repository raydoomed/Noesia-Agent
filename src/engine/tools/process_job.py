"""Windows Job Object for spawned process trees.

MSYS/Git-Bash emulates ``fork``: a child spawned by ``bash -lic`` is a real
Windows process that is not linked to the shell through the kernel's parent
chain, so ``taskkill /T`` cannot reach it and the parent-chain based survivor
probe cannot see it either. A Job Object tracks membership instead of lineage:
every descendant inherits the job, and closing the last handle with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` terminates whatever is still inside.

Non-Windows platforms get no-ops, so callers never branch on the platform.
"""

from __future__ import annotations

import ctypes
import logging
import os

logger = logging.getLogger("tools.process_registry")

_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_IS_WINDOWS = os.name == "nt"


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


def _kernel32():
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.CreateJobObjectW.restype = ctypes.c_void_p
    lib.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    lib.SetInformationJobObject.restype = ctypes.c_int
    lib.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    lib.AssignProcessToJobObject.restype = ctypes.c_int
    lib.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.QueryInformationJobObject.restype = ctypes.c_int
    lib.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
                                              ctypes.c_void_p]
    lib.OpenProcess.restype = ctypes.c_void_p
    lib.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    lib.CloseHandle.restype = ctypes.c_int
    lib.CloseHandle.argtypes = [ctypes.c_void_p]

    return lib


def assign_to_job(pid: int | None) -> int:
    """Put ``pid`` in a fresh kill-on-close job; returns the handle (0 = no job).

    Failure is not fatal: the caller keeps its previous parent-chain cleanup,
    which is only unreliable for MSYS-style forks. The reason is logged because
    "no job" silently means "an escaped child can survive the kill".
    """
    if not _IS_WINDOWS or not pid:
        return 0

    lib = _kernel32()
    job = lib.CreateJobObjectW(None, None)
    if not job:
        logger.warning("CreateJobObject failed (err %s): pid %s runs without a job", ctypes.get_last_error(), pid)
        return 0
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not lib.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                                       ctypes.sizeof(limits)):
        logger.warning("SetInformationJobObject failed (err %s): pid %s runs without a job",
                       ctypes.get_last_error(), pid)
        lib.CloseHandle(job)
        return 0
    handle = lib.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
    if not handle:
        logger.warning("OpenProcess failed (err %s): cannot assign pid %s to a job", ctypes.get_last_error(), pid)
        lib.CloseHandle(job)
        return 0
    try:
        assigned = lib.AssignProcessToJobObject(job, handle)
    finally:
        lib.CloseHandle(handle)
    if not assigned:
        logger.warning("AssignProcessToJobObject failed (err %s): pid %s runs without a job",
                       ctypes.get_last_error(), pid)
        lib.CloseHandle(job)
        return 0

    return int(job)


def active_processes(job: int) -> int:
    """How many processes are still inside the job (-1 = unknown)."""
    if not _IS_WINDOWS or not job:
        return 0

    lib = _kernel32()
    accounting = _BasicAccountingInformation()
    if not lib.QueryInformationJobObject(job, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION, ctypes.byref(accounting),
                                         ctypes.sizeof(accounting), None):
        return -1

    return int(accounting.ActiveProcesses)


def close_job(job: int) -> None:
    """Close the handle; with kill-on-close, every remaining member dies with it."""
    if not _IS_WINDOWS or not job:
        return
    _kernel32().CloseHandle(job)
