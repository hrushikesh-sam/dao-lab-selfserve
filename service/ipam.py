"""IP address management: leases from the pool (B-41, plan/09 §6).

The backend owns every address a reservation's VMs use; the deploy playbook only asks for one
(`POST /internal/leases`, B-42) and sets it statically through cloud-init. Rules:

  * `lease(res, role)` is idempotent: the same (reservation, role) gets the same address back
    for as long as its lease is `leased`, so a replayed playbook never takes a second IP.
    Otherwise it allocates the *lowest* address of the pool that has no live lease.
  * A live lease (`leased` or `quarantined`) blocks its address. The unique index on
    (pool, ip) among live leases (migration 2) is what guarantees no address is given to two
    reservations, even to a caller that bypasses this module.
  * `release(res)` frees a reservation's `leased` addresses once cleanup has proven its VMs are
    gone (B-71). When that could not be proven, `quarantine_reservation(res)` keeps them
    blocked; only the reconciler or an admin frees a quarantined address (`clear`).
  * `mark_conflict(ip)`: something outside daolab answers on a leased address (the playbook's
    ping/arping check). The lease is quarantined so the address is not handed out again, and
    the same role can lease a replacement (migration 7).

Each function writes inside one BEGIN IMMEDIATE transaction unless the caller already holds
one, so parallel deploys leasing at the same time serialise on sqlite's write lock.
"""
import ipaddress
import sqlite3
from contextlib import contextmanager

from . import models

DEFAULT_POOL = "default"     # the single DAOLAB_IP_POOL-backed pool written by `daolab sync`


class IpamError(Exception):
    """No pool, no such lease, or an illegal lease change."""


class PoolExhausted(IpamError):
    """Every address of the pool has a live lease."""


@contextmanager
def _write(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, unless the caller is already inside a transaction."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _pool(conn, name: str):
    row = models.ip_pool_by_name(conn, name)
    if row is None:
        raise IpamError(f"no ip pool {name!r}; set DAOLAB_IP_POOL and run `daolab sync`")
    return row


def addresses(pool) -> list[str]:
    """Every address of a pool row, lowest first."""
    first = int(ipaddress.ip_address(pool["range_start"]))
    last = int(ipaddress.ip_address(pool["range_end"]))
    return [str(ipaddress.ip_address(n)) for n in range(first, last + 1)]


def _lease_by_id(conn, lease_id: int):
    return conn.execute("SELECT * FROM ip_leases WHERE id = ?", (lease_id,)).fetchone()


def _live_by_ip(conn, pool_id: int, ip: str):
    return conn.execute(
        "SELECT * FROM ip_leases WHERE pool_id = ? AND ip = ? AND state != 'released'",
        (pool_id, ip)).fetchone()


def lease(conn: sqlite3.Connection, reservation_id: int, role: str,
          pool: str = DEFAULT_POOL) -> sqlite3.Row:
    """The `leased` row for (reservation, role): the existing one, or a new one on the lowest
    free address. Raises PoolExhausted when every address has a live lease."""
    with _write(conn):
        existing = models.ip_lease_by_role(conn, reservation_id, role)
        if existing is not None:
            return existing
        p = _pool(conn, pool)
        taken = {r["ip"] for r in models.live_leases(conn, p["id"])}
        for ip in addresses(p):
            if ip not in taken:
                lease_id = models.insert(conn, "ip_leases", {
                    "pool_id": p["id"], "ip": ip, "reservation_id": reservation_id,
                    "role": role, "state": "leased"})
                return _lease_by_id(conn, lease_id)
        raise PoolExhausted(f"ip pool {pool!r} ({p['range_start']}-{p['range_end']}) has no "
                            f"free address ({len(taken)} live leases)")


def _set_state(conn, where: str, args: tuple, new_state: str) -> int:
    now = models.utcnow()
    cur = conn.execute(f"UPDATE ip_leases SET state = ?, updated_at = ? WHERE {where}",
                       (new_state, now, *args))
    return cur.rowcount


def release(conn: sqlite3.Connection, reservation_id: int) -> int:
    """Free the reservation's `leased` addresses (cleanup verified clean). Quarantined ones
    stay blocked. Returns how many were released."""
    with _write(conn):
        return _set_state(conn, "reservation_id = ? AND state = 'leased'",
                          (reservation_id,), "released")


def quarantine_reservation(conn: sqlite3.Connection, reservation_id: int) -> int:
    """Block every `leased` address of the reservation: cleanup could not prove its VMs are
    gone, so any of them may still be in use. Returns how many were quarantined."""
    with _write(conn):
        return _set_state(conn, "reservation_id = ? AND state = 'leased'",
                          (reservation_id,), "quarantined")


def quarantine(conn: sqlite3.Connection, ip: str, pool: str = DEFAULT_POOL) -> sqlite3.Row:
    """Quarantine the live lease holding `ip` (a no-op if it already is). Raises IpamError
    when no live lease holds it."""
    ip = str(ipaddress.ip_address(ip))
    with _write(conn):
        p = _pool(conn, pool)
        row = _live_by_ip(conn, p["id"], ip)
        if row is None:
            raise IpamError(f"{ip} has no live lease in pool {pool!r}")
        if row["state"] != "quarantined":
            _set_state(conn, "id = ?", (row["id"],), "quarantined")
        return _lease_by_id(conn, row["id"])


def mark_conflict(conn: sqlite3.Connection, ip: str, pool: str = DEFAULT_POOL) -> sqlite3.Row:
    """Something else answers on `ip`: quarantine its `leased` lease so the address is never
    issued again, which frees the role to lease a replacement. Returns the quarantined lease.
    Raises IpamError when `ip` is not currently leased."""
    ip = str(ipaddress.ip_address(ip))
    with _write(conn):
        p = _pool(conn, pool)
        row = _live_by_ip(conn, p["id"], ip)
        if row is None or row["state"] != "leased":
            raise IpamError(f"{ip} is not leased in pool {pool!r}")
        _set_state(conn, "id = ?", (row["id"],), "quarantined")
        return _lease_by_id(conn, row["id"])


def clear(conn: sqlite3.Connection, reservation_id: int) -> int:
    """Release every live lease of the reservation, quarantined ones included: the reconciler
    or an admin has proven the addresses free (B-71/B-74). Returns how many were released."""
    with _write(conn):
        return _set_state(conn, "reservation_id = ? AND state != 'released'",
                          (reservation_id,), "released")


def lease_object(row) -> dict:
    return {"ip": row["ip"], "reservation_id": row["reservation_id"], "role": row["role"],
            "state": row["state"]}
