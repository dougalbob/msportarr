#!/usr/bin/env python3
"""
Shared JSON persistence for cross-process state
================================================

Several processes (grabber, watcher, WebUI, scheduler) share small JSON state
files in ``/config``. Writing them with a bare ``open(..., "w")`` has two
distinct failure modes, and both have bitten this app:

* a reader in another process can open the file mid-write and see truncated,
  invalid JSON;
* two writers can read the same snapshot, change different things, and the
  second write silently discards the first writer's update. The file stays
  syntactically valid, so nothing looks wrong - the update is just gone.

This module is the one place that contract is implemented, so a caller cannot
accidentally lock only the write phase (the original activity-emitter bug:
the lock was taken on a temporary file, around serialisation only, after the
read had already happened).

The contract:

* **Sidecar lock.** The lock lives in ``<name>.lock`` beside the data file,
  never on the data file itself (which is replaced) and never on a temporary
  file (which is per-write). The lock file is never deleted: unlinking it
  while another process holds it open would create a second lock file and
  silently break mutual exclusion.
* **Whole transaction.** :func:`update_json` holds the exclusive lock across
  load, mutation and commit. A mutation therefore always starts from the
  state the previous writer committed, not from a stale in-memory snapshot.
* **Atomic commit.** New JSON goes to a unique temporary file in the same
  directory, is flushed and fsynced, then ``os.replace`` swaps it over the
  destination. A reader sees the old file or the new one, never a mixture,
  and a failed write leaves the previous valid file readable.
* **Corruption is loud.** An unreadable file is logged at ERROR naming the
  path, then the caller's default is used. Quietly treating corrupt state as
  empty state has repeatedly turned "the file is broken" into hours of
  "why did it do that again" (see ``utils/release_mismatches.py``).

Long-lived in-memory state (the grabber's and watcher's state objects) cannot
use :func:`update_json` for every mutation, because callers also mutate the
state dicts directly between saves. For those, :func:`three_way_merge`
reconciles the writer's changes against whatever another process committed
since this process loaded its snapshot, so ``save()`` becomes a merge rather
than an overwrite. Deletions are honoured: a key this process removed stays
removed, unless the other process changed that same key in the meantime, in
which case the newer change is kept rather than destroyed.
"""

from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TypeVar

logger = logging.getLogger("msportarr.json_store")

T = TypeVar("T")


def lock_path_for(path: Path) -> Path:
    """Sidecar lock file for ``path``.

    ``grabber_state.motogp.json`` -> ``grabber_state.motogp.lock``. The lock
    follows the data file's name, so a namespaced store gets a namespaced lock
    and two series never serialise against each other's sidecar.
    """
    path = Path(path)
    if path.suffix:
        return path.with_suffix(".lock")
    return path.with_name(path.name + ".lock")


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """
    Exclusive lock on the sidecar file for ``path``, held for the block.

    ``path`` is the *data* file; the lock file is derived from it so every
    caller locks the same file. The parent directory is created because a
    first run may lock before anything else has made ``/config``.
    """
    lock_path = lock_path_for(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def read_json(path: Path, default: T, log: Optional[logging.Logger] = None) -> T:
    """
    Read JSON from ``path``, returning ``default`` when it cannot be used.

    A missing file is normal (first run) and quiet. A file that exists but
    does not parse is logged at ERROR naming the path: callers fall back to
    ``default``, and the operator needs to know the on-disk state is being
    ignored until it is fixed or removed.
    """
    log = log or logger
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(
            "Could not read %s: %s. Ignoring the stored state until this "
            "file is fixed or removed.",
            path, exc,
        )
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    """
    Replace ``path`` with ``payload`` atomically. Caller holds the lock.

    The temporary name is unique per write: a shared temp name was part of
    the original activity bug, because two writers could interleave on the
    same file and rename each other's half-written output.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", dir=str(path.parent),
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    tmp = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_json(path: Path, payload: Any) -> None:
    """Locked, atomic write of a complete new value for ``path``."""
    with file_lock(path):
        atomic_write_json(path, payload)


def update_json(
    path: Path,
    default_factory: Callable[[], T],
    mutator: Callable[[T], Any],
    log: Optional[logging.Logger] = None,
) -> Any:
    """
    Run one locked read-modify-write transaction on ``path``.

    The file is (re)loaded *after* the lock is taken, ``mutator`` changes the
    loaded value in place, and the result is committed before the lock is
    released - so concurrent updaters in different processes each build on
    the previous one's committed state and no update is lost. Returns
    whatever ``mutator`` returns. Write failures propagate: silently dropping
    an update is the failure mode this module exists to prevent, so callers
    decide (and log) what a failed write means for them.
    """
    log = log or logger
    with file_lock(path):
        data = read_json(path, default_factory(), log=log)
        result = mutator(data)
        atomic_write_json(path, data)
        return result


def reload_under_lock(path: Path, default: T, log: Optional[logging.Logger] = None) -> T:
    """Current on-disk value of ``path``, read while holding its lock."""
    with file_lock(path):
        return read_json(path, default, log=log)


_MISSING = object()


def three_way_merge(base: Any, ours: Any, disk: Any) -> Any:
    """
    Merge one writer's changes into the state another writer committed.

    ``base`` is the snapshot this process loaded, ``ours`` is its current
    in-memory state, and ``disk`` is what is on disk now (loaded under the
    lock). The result keeps:

    * keys only the other process added (we never saw them, so an overwrite
      from our snapshot would have deleted them - the lost-update bug);
    * keys we added or changed relative to ``base`` (our update wins for
      keys we actually touched);
    * deletions we made relative to ``base``, unless the other process
      changed that same key after our snapshot, in which case their newer
      value survives rather than being destroyed by our stale delete.

    Dicts merge recursively; anything else is treated as a leaf where
    "changed relative to base" decides whose value wins.
    """
    if isinstance(base, dict) and isinstance(ours, dict) and isinstance(disk, dict):
        merged = dict(disk)
        for key, our_value in ours.items():
            base_value = base.get(key, _MISSING)
            if base_value is _MISSING:
                merged[key] = copy.deepcopy(our_value)
            elif our_value != base_value:
                disk_value = disk.get(key, _MISSING)
                merged[key] = three_way_merge(
                    base_value, our_value,
                    copy.deepcopy(disk_value) if disk_value is not _MISSING else copy.deepcopy(base_value),
                )
        for key, base_value in base.items():
            if key in ours:
                continue
            disk_value = disk.get(key, _MISSING)
            if disk_value is _MISSING:
                continue
            if disk_value == base_value:
                # We deleted it and nobody else touched it: deletion stands.
                merged.pop(key, None)
            # else: the other process changed it after our snapshot; keep theirs.
        return merged
    if ours != base:
        return copy.deepcopy(ours)
    return copy.deepcopy(disk)
