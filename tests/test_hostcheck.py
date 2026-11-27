"""B-107 E-2 host checks: swap safety and the snapshots-in-use helper."""
import os

import pytest

from service import hostcheck

OK_NONE = "swap ok: none"
OK_SAFE = "swap ok: encrypted or zram"
UNSAFE = "swap unsafe: disable swap, use zram or dm-crypt"
POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")


@pytest.fixture
def swaps(tmp_path):
    path = tmp_path / "swaps"
    path.write_text("Filename\tType\t\tSize\tUsed\tPriority\n")
    return path


def _add_swap(swaps, name, kind="partition"):
    with open(swaps, "a") as fh:
        fh.write(f"{name}\t{kind}\t1024\t0\t-2\n")


@pytest.fixture
def sys_block(tmp_path):
    root = tmp_path / "block"
    root.mkdir()
    return root


def _dm_uuid(sys_block, dm_name, value):
    path = sys_block / dm_name / "dm" / "uuid"
    path.parent.mkdir(parents=True)
    path.write_text(value + "\n")
    return path


def test_no_swap_is_safe(swaps, sys_block):
    assert hostcheck.swap_safe(str(swaps), str(sys_block)) == (True, OK_NONE)


def test_zram_is_safe(swaps, sys_block):
    _add_swap(swaps, "/dev/zram0")
    _add_swap(swaps, "/dev/zram1")
    assert hostcheck.swap_safe(str(swaps), str(sys_block)) == (True, OK_SAFE)


def test_dm_crypt_is_safe(swaps, sys_block):
    _dm_uuid(sys_block, "dm-0", "CRYPT-LUKS2-abcdef")
    _add_swap(swaps, "/dev/dm-0")
    assert hostcheck.swap_safe(str(swaps), str(sys_block)) == (True, OK_SAFE)


@POSIX
def test_mapper_symlink_to_dm_crypt_is_safe(tmp_path, swaps, sys_block):
    _dm_uuid(sys_block, "dm-0", "CRYPT-PLAIN")
    mapper = tmp_path / "mapper"
    mapper.mkdir()
    os.symlink(sys_block / "dm-0", mapper / "cryptswap")
    _add_swap(swaps, str(mapper / "cryptswap"))
    assert hostcheck.swap_safe(str(swaps), str(sys_block)) == (True, OK_SAFE)


@pytest.mark.parametrize("name", ["/swapfile", "/home/lab/swap", "swapfile"])
def test_swapfile_is_unsafe(swaps, sys_block, name):
    _add_swap(swaps, name, "file")
    assert hostcheck.swap_safe(str(swaps), str(sys_block)) == (False, UNSAFE)


def test_plain_partition_is_unsafe(swaps, sys_block):
    _add_swap(swaps, "/dev/sda2")
    assert hostcheck.swap_safe(str(swaps), str(sys_block)) == (False, UNSAFE)


def test_plain_dm_is_unsafe(swaps, sys_block):
    _dm_uuid(sys_block, "dm-0", "LVM-abc")
    _add_swap(swaps, "/dev/dm-0")
    assert hostcheck.swap_safe(str(swaps), str(sys_block)) == (False, UNSAFE)


@POSIX
def test_mapper_symlink_to_plain_device_is_unsafe(tmp_path, swaps, sys_block):
    mapper = tmp_path / "mapper"
    mapper.mkdir()
    os.symlink(sys_block / "sda2", mapper / "plain")
    _add_swap(swaps, str(mapper / "plain"))
    assert hostcheck.swap_safe(str(swaps), str(sys_block)) == (False, UNSAFE)


def test_one_unsafe_device_taints_otherwise_safe_swap(swaps, sys_block):
    _dm_uuid(sys_block, "dm-0", "CRYPT-LUKS2-abcdef")
    _add_swap(swaps, "/dev/dm-0")
    _add_swap(swaps, "/dev/sda2")
    assert hostcheck.swap_safe(str(swaps), str(sys_block)) == (False, UNSAFE)


def test_missing_swaps_file_fails_closed(tmp_path, sys_block):
    assert hostcheck.swap_safe(str(tmp_path / "missing"), str(sys_block)) == (False, UNSAFE)


def test_require_safe_swap_raises_fixed_message(swaps, sys_block):
    _add_swap(swaps, "/swapfile", "file")
    with pytest.raises(hostcheck.HostCheckError) as exc:
        hostcheck.require_safe_swap(proc_swaps=str(swaps), sys_block=str(sys_block))
    assert str(exc.value) == UNSAFE


def test_require_safe_swap_accepts_safe_swap(swaps, sys_block):
    _dm_uuid(sys_block, "dm-0", "CRYPT-LUKS2-abcdef")
    _add_swap(swaps, "/dev/dm-0")
    hostcheck.require_safe_swap(proc_swaps=str(swaps), sys_block=str(sys_block))


def test_snapshots_in_use(db):
    from tests.conftest import mk_setup
    assert hostcheck.snapshots_in_use(db) is False
    sid = mk_setup(db, "tb-01", ["e810"])
    assert hostcheck.snapshots_in_use(db) is False
    db.execute("UPDATE setups SET snapshot_enabled = 1 WHERE id = ?", (sid,))
    assert hostcheck.snapshots_in_use(db) is True
