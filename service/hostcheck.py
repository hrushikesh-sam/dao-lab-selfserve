"""B-107 E-2 host checks: swap safety and the snapshots-in-use gate."""
import os
import re

OK_NONE = "swap ok: none"
OK_SAFE = "swap ok: encrypted or zram"
UNSAFE = "swap unsafe: disable swap, use zram or dm-crypt"

_DM_NAME = re.compile(r"dm-\d+")


class HostCheckError(Exception):
    pass


def _dm_crypt(dm_name: str, sys_block: str) -> bool:
    if not _DM_NAME.fullmatch(dm_name):
        return False
    try:
        with open(os.path.join(sys_block, dm_name, "dm", "uuid")) as fh:
            return fh.read().strip().startswith("CRYPT-")
    except OSError:
        return False


def _safe_device(name: str, sys_block: str) -> bool:
    if name.startswith("/dev/zram"):
        return True
    base = os.path.basename(os.path.realpath(name))
    if _DM_NAME.fullmatch(base):
        return _dm_crypt(base, sys_block)
    return False


def swap_safe(proc_swaps: str = "/proc/swaps", sys_block: str = "/sys/block") -> tuple[bool, str]:
    try:
        with open(proc_swaps) as fh:
            lines = fh.readlines()
    except OSError:
        return False, UNSAFE
    devices = [parts[0] for line in lines if (parts := line.split()) and parts[0] != "Filename"]
    if not devices:
        return True, OK_NONE
    if all(_safe_device(device, sys_block) for device in devices):
        return True, OK_SAFE
    return False, UNSAFE


def require_safe_swap(proc_swaps: str = "/proc/swaps", sys_block: str = "/sys/block") -> None:
    ok, reason = swap_safe(proc_swaps, sys_block)
    if not ok:
        raise HostCheckError(reason)


def snapshots_in_use(conn) -> bool:
    row = conn.execute("SELECT 1 FROM setups WHERE snapshot_enabled = 1 LIMIT 1").fetchone()
    return row is not None
