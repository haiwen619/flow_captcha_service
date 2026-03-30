import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite

from src.core.database import Database


class LogCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "cleanup.sqlite3")
        await self.db.init_db()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_clear_runtime_logs_preserves_quota_usage(self):
        raw_key, api_key = await self.db.create_api_key("cleanup-test", 3)
        self.assertTrue(raw_key.startswith("fcs_"))

        consumed, message = await self.db.consume_api_key_quota(int(api_key["id"]), session_id="session-1")
        self.assertTrue(consumed, message)
        await self.db.create_job_log(
            session_id="session-1",
            api_key_id=int(api_key["id"]),
            project_id="demo-project",
            action="IMAGE_GENERATION",
            status="success",
            error_reason=None,
            duration_ms=123,
        )

        result = await self.db.clear_runtime_logs()
        self.assertGreaterEqual(int(result["captcha_jobs"]), 1)

        updated_key = await self.db.get_api_key(int(api_key["id"]))
        self.assertIsNotNone(updated_key)
        self.assertEqual(int(updated_key["quota_used"]), 1)
        self.assertEqual(int(updated_key["quota_remaining"]), 2)

        async with self.db._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT COUNT(*) AS total FROM session_quota_events")
            row = await cursor.fetchone()
            self.assertGreater(int(row["total"] or 0), 0)

            cursor = await conn.execute("SELECT COUNT(*) AS total FROM captcha_jobs")
            row = await cursor.fetchone()
            self.assertEqual(int(row["total"] or 0), 0)

    async def test_periodic_cleanup_uses_checkpoint_without_small_vacuum(self):
        _, api_key = await self.db.create_api_key("cleanup-checkpoint", 3)
        await self.db.create_job_log(
            session_id="session-2",
            api_key_id=int(api_key["id"]),
            project_id="demo-project",
            action="IMAGE_GENERATION",
            status="success",
            error_reason=None,
            duration_ms=99,
        )

        checkpoint_mock = AsyncMock()
        with patch.object(self.db, "_checkpoint_and_vacuum_logs", checkpoint_mock):
            result = await self.db.clear_runtime_logs()

        self.assertEqual(int(result["captcha_jobs"]), 1)
        checkpoint_mock.assert_awaited_once()
        self.assertEqual(checkpoint_mock.await_args.kwargs["reason"], "periodic")
        self.assertFalse(bool(checkpoint_mock.await_args.kwargs["vacuum"]))

    def test_periodic_vacuum_threshold_allows_low_frequency_vacuum(self):
        self.db._last_log_vacuum_monotonic = 0.0
        self.assertFalse(self.db._should_vacuum_periodic_logs(1))
        self.assertTrue(
            self.db._should_vacuum_periodic_logs(self.db.PERIODIC_LOG_VACUUM_ROW_THRESHOLD)
        )


if __name__ == "__main__":
    unittest.main()
