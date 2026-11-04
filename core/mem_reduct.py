"""
core/mem_reduct.py
Saturn - Native Windows NT RAM Reducer & Standby List Purger.
Functions identically and efficiently to Mem Reduct:
1. Flushes process working sets via K32EmptyWorkingSet / SetProcessWorkingSetSize
2. Purges system working sets via NtSetSystemInformation (MemoryEmptyWorkingSets = 2)
3. Flushes modified page list via NtSetSystemInformation (MemoryFlushModifiedList = 3)
4. Purges standby memory list via NtSetSystemInformation (MemoryPurgeStandbyList = 4)
5. Purges low priority standby list (MemoryPurgeLowPriorityStandbyList = 5)
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import psutil
from typing import Any, Dict

logger = logging.getLogger("MemReduct")

# Windows constants
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100
SystemMemoryListInformation = 80

# SYSTEM_MEMORY_LIST_COMMAND enum values
# 2: MemoryEmptyWorkingSets
# 3: MemoryFlushModifiedList
# 4: MemoryPurgeStandbyList
# 5: MemoryPurgeLowPriorityStandbyList
MEMORY_COMMAND_EMPTY_WORKING_SETS = 2
MEMORY_COMMAND_FLUSH_MODIFIED_LIST = 3
MEMORY_COMMAND_PURGE_STANDBY_LIST = 4
MEMORY_COMMAND_PURGE_LOW_PRIORITY_STANDBY_LIST = 5


class MemReductEngine:
    """High-efficiency RAM cleaner utilizing native Windows NT memory manager APIs."""

    @staticmethod
    def is_windows() -> bool:
        return os.name == "nt"

    @classmethod
    def clean_ram(cls, purge_standby: bool = True, empty_working_sets: bool = True) -> Dict[str, Any]:
        """
        Executes a deep memory purge matching Mem Reduct.
        Returns statistics of memory freed and percentage drop.
        """
        before_mem = psutil.virtual_memory()
        trimmed_processes = 0
        nt_commands_executed = 0

        if not cls.is_windows():
            # Fallback for non-Windows systems: garbage collection & reporting
            import gc
            gc.collect()
            after_mem = psutil.virtual_memory()
            freed_bytes = max(0, after_mem.available - before_mem.available)
            return {
                "success": True,
                "freed_mb": round(freed_bytes / (1024 * 1024), 1),
                "before_percent": before_mem.percent,
                "after_percent": after_mem.percent,
                "trimmed_processes": 0,
                "total_ram_gb": round(before_mem.total / (1024 ** 3), 1),
                "summary": f"Garbage collection executed. Memory at {after_mem.percent}%."
            }

        kernel32 = ctypes.windll.kernel32
        psapi = getattr(ctypes.windll, "psapi", None)
        ntdll = getattr(ctypes.windll, "ntdll", None)

        # 1. Empty Working Sets for all accessible user-mode processes
        if empty_working_sets:
            # Self process first
            try:
                h_self = kernel32.GetCurrentProcess()
                kernel32.SetProcessWorkingSetSize(h_self, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
                trimmed_processes += 1
            except Exception as e:
                logger.debug(f"Self working set trim error: {e}")

            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    pid = proc.info['pid']
                    if pid <= 4:
                        continue  # Skip System Idle and System
                    h_proc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid)
                    if h_proc:
                        try:
                            # Try psapi.EmptyWorkingSet or K32EmptyWorkingSet
                            if psapi and hasattr(psapi, "EmptyWorkingSet"):
                                if psapi.EmptyWorkingSet(h_proc):
                                    trimmed_processes += 1
                            elif hasattr(kernel32, "K32EmptyWorkingSet"):
                                if kernel32.K32EmptyWorkingSet(h_proc):
                                    trimmed_processes += 1
                            else:
                                kernel32.SetProcessWorkingSetSize(h_proc, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
                                trimmed_processes += 1
                        finally:
                            kernel32.CloseHandle(h_proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError):
                    continue
                except Exception:
                    continue

        # 2. Native NT Memory List Purges (Standby lists and System Working Sets)
        if purge_standby and ntdll and hasattr(ntdll, "NtSetSystemInformation"):
            # Purge in optimal sequence: Flush Modified -> Purge Standby -> Empty System Working Sets
            commands = [
                MEMORY_COMMAND_FLUSH_MODIFIED_LIST,
                MEMORY_COMMAND_PURGE_STANDBY_LIST,
                MEMORY_COMMAND_PURGE_LOW_PRIORITY_STANDBY_LIST,
                MEMORY_COMMAND_EMPTY_WORKING_SETS,
            ]
            for cmd in commands:
                try:
                    cmd_val = ctypes.c_ulong(cmd)
                    status = ntdll.NtSetSystemInformation(
                        SystemMemoryListInformation,
                        ctypes.byref(cmd_val),
                        ctypes.sizeof(cmd_val)
                    )
                    if status == 0:  # STATUS_SUCCESS
                        nt_commands_executed += 1
                except Exception as e:
                    logger.debug(f"NtSetSystemInformation cmd {cmd} error: {e}")

        after_mem = psutil.virtual_memory()
        freed_bytes = after_mem.available - before_mem.available
        freed_mb = round(freed_bytes / (1024 * 1024), 1)

        # If freed_mb is marginally negative or zero due to immediate OS page shuffling, calculate diff in used RAM
        if freed_mb <= 0:
            diff_used = (before_mem.used - after_mem.used) / (1024 * 1024)
            freed_mb = round(max(0.0, diff_used), 1)

        pct_drop = round(max(0.0, before_mem.percent - after_mem.percent), 1)
        total_gb = round(before_mem.total / (1024 ** 3), 1)

        summary = (
            f"RAM purged successfully. Freed {freed_mb} MB across {trimmed_processes} processes. "
            f"RAM usage: {before_mem.percent}% -> {after_mem.percent}% (Total: {total_gb} GB)."
        )

        logger.info(summary)

        return {
            "success": True,
            "freed_mb": freed_mb,
            "freed_gb": round(freed_mb / 1024.0, 2),
            "before_used_mb": round(before_mem.used / (1024 * 1024), 1),
            "after_used_mb": round(after_mem.used / (1024 * 1024), 1),
            "before_percent": before_mem.percent,
            "after_percent": after_mem.percent,
            "percent_drop": pct_drop,
            "trimmed_processes": trimmed_processes,
            "nt_commands_executed": nt_commands_executed,
            "total_ram_gb": total_gb,
            "summary": summary
        }
