from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class YesCaptchaTaskRecord:
    task_id: str
    owner_scope: str
    task_type: str
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))
    status: str = "processing"
    solution: Optional[Dict[str, Any]] = None
    error_id: int = 0
    error_code: str = ""
    error_description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class YesCaptchaTaskManager:
    def __init__(self, *, task_ttl_seconds: int = 1200, cleanup_interval_seconds: int = 60):
        self._task_ttl_seconds = max(60, int(task_ttl_seconds or 1200))
        self._cleanup_interval_seconds = max(10, int(cleanup_interval_seconds or 60))
        self._tasks: Dict[str, YesCaptchaTaskRecord] = {}
        self._workers: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._task_sequence = max(1, int(time.time() * 1000))
        self._next_cleanup_monotonic = time.monotonic()

    async def start(self):
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def close(self):
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._cleanup_task = None

        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()

        for worker in workers:
            if not worker.done():
                worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def create_task(self, *, owner_scope: str, task_type: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        self._task_sequence += 1
        record = YesCaptchaTaskRecord(
            task_id=str(self._task_sequence),
            owner_scope=str(owner_scope or "").strip(),
            task_type=str(task_type or "").strip(),
            metadata=dict(metadata or {}),
        )
        async with self._lock:
            self._maybe_purge_expired_locked()
            self._tasks[record.task_id] = record
        return record.task_id

    async def register_worker(self, task_id: str, worker: asyncio.Task):
        async with self._lock:
            self._workers[str(task_id)] = worker
        worker.add_done_callback(lambda _: asyncio.create_task(self._forget_worker(str(task_id))))

    async def get_task(self, task_id: str, *, owner_scope: str) -> Optional[YesCaptchaTaskRecord]:
        async with self._lock:
            record = self._tasks.get(str(task_id or "").strip())
            if record is None:
                return None
            if record.owner_scope != str(owner_scope or "").strip():
                return None
            if self._is_record_expired(record):
                self._tasks.pop(record.task_id, None)
                self._workers.pop(record.task_id, None)
                return None
            return YesCaptchaTaskRecord(
                task_id=record.task_id,
                owner_scope=record.owner_scope,
                task_type=record.task_type,
                created_at=record.created_at,
                updated_at=record.updated_at,
                status=record.status,
                solution=dict(record.solution or {}) if record.solution else None,
                error_id=record.error_id,
                error_code=record.error_code,
                error_description=record.error_description,
                metadata=dict(record.metadata or {}),
            )

    async def mark_ready(
        self,
        task_id: str,
        *,
        owner_scope: str,
        solution: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return await self._update_task(
            task_id,
            owner_scope=owner_scope,
            status="ready",
            solution=solution,
            metadata=metadata,
        )

    async def mark_error(
        self,
        task_id: str,
        *,
        owner_scope: str,
        error_id: int,
        error_code: str,
        error_description: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return await self._update_task(
            task_id,
            owner_scope=owner_scope,
            status="error",
            error_id=max(1, int(error_id or 1)),
            error_code=str(error_code or "").strip(),
            error_description=str(error_description or "").strip(),
            metadata=metadata,
        )

    async def _update_task(
        self,
        task_id: str,
        *,
        owner_scope: str,
        status: str,
        solution: Optional[Dict[str, Any]] = None,
        error_id: int = 0,
        error_code: str = "",
        error_description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        async with self._lock:
            self._maybe_purge_expired_locked()
            record = self._tasks.get(str(task_id or "").strip())
            if record is None or record.owner_scope != str(owner_scope or "").strip():
                return False
            if self._is_record_expired(record):
                self._tasks.pop(record.task_id, None)
                self._workers.pop(record.task_id, None)
                return False
            record.status = str(status or "processing").strip() or "processing"
            record.updated_at = int(time.time())
            record.solution = dict(solution or {}) if solution else None
            record.error_id = max(0, int(error_id or 0))
            record.error_code = str(error_code or "").strip()
            record.error_description = str(error_description or "").strip()
            if metadata:
                record.metadata.update(dict(metadata))
            return True

    async def _forget_worker(self, task_id: str):
        async with self._lock:
            self._workers.pop(str(task_id or "").strip(), None)

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval_seconds)
                async with self._lock:
                    self._purge_expired_locked(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    def _maybe_purge_expired_locked(self):
        if time.monotonic() < self._next_cleanup_monotonic:
            return
        self._purge_expired_locked()

    def _is_record_expired(self, record: YesCaptchaTaskRecord) -> bool:
        expire_before = int(time.time()) - self._task_ttl_seconds
        return int(record.updated_at or record.created_at or 0) < expire_before

    def _purge_expired_locked(self, *, force: bool = False):
        if not force and time.monotonic() < self._next_cleanup_monotonic:
            return
        expired_ids = [
            task_id
            for task_id, record in self._tasks.items()
            if self._is_record_expired(record)
        ]
        for task_id in expired_ids:
            self._tasks.pop(task_id, None)
            self._workers.pop(task_id, None)
        self._next_cleanup_monotonic = time.monotonic() + self._cleanup_interval_seconds
