"""
state_manager.py
================

Reference implementation: a small, dependency-free checkpoint / resume manager
for long-running batch jobs.

Design goals
------------
* **Durable**   - every state transition is persisted immediately using an
                  atomic write (temp file + ``os.replace``), so a crash or power
                  loss never leaves a half-written checkpoint.
* **Resumable** - completed items are never processed again; failed items can
                  be retried after the operator corrects the input.
* **Crash safe**- items that were in flight when the process died are treated
                  as pending on the next load.
* **Thread safe** - guarded by a re-entrant lock so a UI thread can read the
                  summary while a worker thread updates it.
* **Never destructive** - a checkpoint that belongs to another job is never
                  overwritten (an error is raised), and an unreadable file is
                  moved aside as ``*.corrupt`` instead of being discarded.

This is a *reference sample* showing the pattern, not production source code.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional


class CheckpointConflictError(RuntimeError):
    """The checkpoint file on disk belongs to a different job."""


class ItemStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class ItemState:
    item_id: str
    status: ItemStatus = ItemStatus.PENDING
    attempts: int = 0
    error: Optional[str] = None
    updated_at: float = 0.0


class StateManager:
    """Persist per-item progress and decide what still needs to run."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | os.PathLike, job_id: str) -> None:
        self._path = Path(path)
        self._job_id = job_id
        self._items: Dict[str, ItemState] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ setup

    @classmethod
    def load_or_create(
        cls, path: str | os.PathLike, job_id: str, item_ids: Iterable[str]
    ) -> "StateManager":
        """Resume an existing checkpoint for ``job_id`` or start a new one.

        A checkpoint that belongs to a different job is never merged or
        overwritten: :class:`CheckpointConflictError` is raised so that
        unrelated runs cannot contaminate (or destroy) each other.
        """
        manager = cls(path, job_id)
        manager._load_existing()
        manager.register(item_ids)
        return manager

    def register(self, item_ids: Iterable[str]) -> int:
        """Add unseen items as ``PENDING``; existing items keep their state.

        Calling this again with a corrected list is how an operator fixes
        input and resumes: finished items stay finished, new ones are queued.
        Returns the number of items added.
        """
        added = 0
        with self._lock:
            for item_id in item_ids:
                if item_id not in self._items:
                    self._items[item_id] = ItemState(item_id, updated_at=time.time())
                    added += 1
            self._persist()
        return added

    # ------------------------------------------------------------ transitions

    def mark_in_progress(self, item_id: str) -> None:
        self._transition(item_id, ItemStatus.IN_PROGRESS, bump_attempts=True)

    def mark_success(self, item_id: str) -> None:
        self._transition(item_id, ItemStatus.SUCCESS, error=None)

    def mark_failed(self, item_id: str, error: str) -> None:
        self._transition(item_id, ItemStatus.FAILED, error=error[:300])

    def clear(self) -> None:
        """Delete the checkpoint file (call once the whole job has finished)."""
        with self._lock:
            self._path.unlink(missing_ok=True)

    def reset_failed(self) -> int:
        """Move every failed item back to pending (the "Resume" action)."""
        count = 0
        with self._lock:
            for state in self._items.values():
                if state.status is ItemStatus.FAILED:
                    state.status, state.error, state.updated_at = ItemStatus.PENDING, None, time.time()
                    count += 1
            self._persist()
        return count

    # ---------------------------------------------------------------- queries

    def is_done(self, item_id: str) -> bool:
        with self._lock:
            state = self._items.get(item_id)
            return state is not None and state.status is ItemStatus.SUCCESS

    def pending_items(self) -> List[str]:
        """Items that still need work, in their original order."""
        with self._lock:
            return [s.item_id for s in self._items.values() if s.status is ItemStatus.PENDING]

    def iter_work(self) -> Iterator[str]:
        """Yield items to process; each is marked in-progress before it is yielded."""
        for item_id in self.pending_items():
            self.mark_in_progress(item_id)
            yield item_id

    def summary(self) -> Dict[str, int]:
        with self._lock:
            counts = {status.value: 0 for status in ItemStatus}
            for state in self._items.values():
                counts[state.status.value] += 1
            counts["total"] = len(self._items)
            return counts

    def failures(self) -> Dict[str, str]:
        with self._lock:
            return {s.item_id: s.error or "" for s in self._items.values() if s.status is ItemStatus.FAILED}

    # --------------------------------------------------------------- internals

    def _transition(
        self, item_id: str, status: ItemStatus, *, error: Optional[str] = None, bump_attempts: bool = False
    ) -> None:
        with self._lock:
            state = self._items.setdefault(item_id, ItemState(item_id))
            state.status, state.error, state.updated_at = status, error, time.time()
            if bump_attempts:
                state.attempts += 1
            self._persist()

    def _load_existing(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return  # first run
        except (json.JSONDecodeError, OSError):
            # Unreadable file: keep it for inspection and start clean.
            try:
                os.replace(self._path, self._path.with_suffix(self._path.suffix + ".corrupt"))
            except OSError:
                pass
            return
        if data.get("schema") != self.SCHEMA_VERSION:
            return
        if data.get("job_id") != self._job_id:
            raise CheckpointConflictError(
                f"{self._path} belongs to job {data.get('job_id')!r}, not {self._job_id!r}; "
                "use a separate checkpoint path per job or call clear() first."
            )
        for raw in data.get("items", []):
            state = ItemState(
                item_id=raw["item_id"],
                status=ItemStatus(raw["status"]),
                attempts=int(raw.get("attempts", 0)),
                error=raw.get("error"),
                updated_at=float(raw.get("updated_at", 0.0)),
            )
            if state.status is ItemStatus.IN_PROGRESS:
                state.status = ItemStatus.PENDING  # interrupted mid-flight -> redo it
            self._items[state.item_id] = state

    def _persist(self) -> None:
        payload = {
            "schema": self.SCHEMA_VERSION,
            "job_id": self._job_id,
            "saved_at": time.time(),
            "items": [{**asdict(s), "status": s.status.value} for s in self._items.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=self._path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)  # atomic on POSIX and Windows
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


if __name__ == "__main__":
    # Tiny demonstration: the first run "crashes" halfway, the second resumes.
    demo_path = Path(tempfile.gettempdir()) / "state_manager_demo.json"
    demo_path.unlink(missing_ok=True)
    items = [f"item-{n}" for n in range(1, 6)]

    run1 = StateManager.load_or_create(demo_path, "demo-job", items)
    for item in run1.iter_work():
        if item == "item-3":
            run1.mark_failed(item, "simulated transient error")
        else:
            run1.mark_success(item)
        if item == "item-4":
            break  # simulated crash: item-5 was never touched
    print("after run 1:", run1.summary())

    run2 = StateManager.load_or_create(demo_path, "demo-job", items)
    run2.reset_failed()  # operator fixed the cause and pressed "Resume"
    for item in run2.iter_work():
        run2.mark_success(item)
    print("after resume:", run2.summary())
