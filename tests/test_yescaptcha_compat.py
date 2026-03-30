import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import yescaptcha
from src.core.auth import set_database
from src.core.database import Database
from src.services.yescaptcha_manager import YesCaptchaTaskManager, YesCaptchaTaskRecord


class FakeRuntime:
    def __init__(self):
        self.token = "default-token"
        self.fingerprint = {"user_agent": "UA-Test"}
        self.calls = []

    async def custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str,
        enterprise: bool,
        captcha_type: str = "recaptcha_v3",
        is_invisible: bool = True,
    ):
        self.calls.append(
            {
                "website_url": website_url,
                "website_key": website_key,
                "action": action,
                "enterprise": enterprise,
                "captcha_type": captcha_type,
                "is_invisible": is_invisible,
            }
        )
        return {
            "token": self.token,
            "fingerprint": dict(self.fingerprint),
            "node_name": "test-node",
            "browser_id": 9,
        }


class YesCaptchaCompatTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env_patcher = patch.dict(os.environ, {"FCS_CLUSTER_ROLE": "standalone"}, clear=False)
        self.env_patcher.start()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "yescaptcha.db"
        self.db = Database(self.db_path)
        await self.db.init_db()
        set_database(self.db)

        self.runtime = FakeRuntime()
        self.task_manager = YesCaptchaTaskManager(task_ttl_seconds=300, cleanup_interval_seconds=30)
        await self.task_manager.start()
        yescaptcha.set_dependencies(self.db, self.runtime, None, self.task_manager)

        self.raw_key, self.api_key = await self.db.create_api_key("yes-service", 3)

    async def asyncTearDown(self):
        await self.task_manager.close()
        await self.db.close()
        self.temp_dir.cleanup()
        self.env_patcher.stop()

    async def _run_task(self, raw_task):
        task_payload = yescaptcha._normalize_task(raw_task)
        owner_scope = yescaptcha._owner_scope(self.api_key)
        task_id = await self.task_manager.create_task(
            owner_scope=owner_scope,
            task_type=task_payload["task_type"],
            metadata={"raw_task": task_payload["raw_task"]},
        )
        worker = asyncio.create_task(yescaptcha._process_task(task_id, owner_scope, dict(self.api_key), dict(task_payload)))
        await self.task_manager.register_worker(task_id, worker)
        await asyncio.wait_for(worker, timeout=5)
        record = await self.task_manager.get_task(task_id, owner_scope=owner_scope)
        self.assertIsNotNone(record)
        return task_id, record

    async def test_recaptcha_v3_task_runs_and_consumes_quota(self):
        self.runtime.token = "recaptcha-token"
        self.runtime.fingerprint = {"user_agent": "Recaptcha-UA"}

        task_id, record = await self._run_task(
            {
                "type": "RecaptchaV3TaskProxyless",
                "websiteURL": "https://example.com/login",
                "websiteKey": "site-key",
                "pageAction": "login",
            }
        )

        payload = yescaptcha._task_result_payload(record)
        self.assertEqual(payload["errorId"], 0)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(str(payload["taskId"]), str(task_id))
        self.assertEqual(payload["solution"]["gRecaptchaResponse"], "recaptcha-token")
        self.assertEqual(payload["solution"]["token"], "recaptcha-token")
        self.assertEqual(payload["solution"]["userAgent"], "Recaptcha-UA")

        self.assertEqual(self.runtime.calls[0]["captcha_type"], "recaptcha_v3")
        self.assertEqual(self.runtime.calls[0]["action"], "login")
        fresh_api_key = await self.db.get_api_key(int(self.api_key["id"]))
        self.assertEqual(int(fresh_api_key["quota_remaining"] or 0), 2)

    async def test_turnstile_task_returns_turnstile_solution_shape(self):
        self.runtime.token = "turnstile-token"
        self.runtime.fingerprint = {"user_agent": "Turnstile-UA"}

        _, record = await self._run_task(
            {
                "type": "TurnstileTaskProxyless",
                "websiteURL": "https://example.com/cf",
                "websiteKey": "cf-site-key",
                "action": "managed",
            }
        )

        payload = yescaptcha._task_result_payload(record)
        self.assertEqual(payload["solution"]["token"], "turnstile-token")
        self.assertEqual(payload["solution"]["userAgent"], "Turnstile-UA")
        self.assertNotIn("gRecaptchaResponse", payload["solution"])
        self.assertEqual(self.runtime.calls[0]["captcha_type"], "turnstile")

    async def test_get_task_only_evicts_requested_expired_record(self):
        now_ts = int(time.time())
        self.task_manager._task_ttl_seconds = 1
        self.task_manager._tasks["expired-task"] = YesCaptchaTaskRecord(
            task_id="expired-task",
            owner_scope="service:1",
            task_type="RecaptchaV3TaskProxyless",
            created_at=now_ts - 5,
            updated_at=now_ts - 5,
        )
        self.task_manager._tasks["fresh-task"] = YesCaptchaTaskRecord(
            task_id="fresh-task",
            owner_scope="service:1",
            task_type="RecaptchaV3TaskProxyless",
            created_at=now_ts,
            updated_at=now_ts,
        )

        fresh_record = await self.task_manager.get_task("fresh-task", owner_scope="service:1")
        expired_record = await self.task_manager.get_task("expired-task", owner_scope="service:1")

        self.assertIsNotNone(fresh_record)
        self.assertIsNone(expired_record)
        self.assertIn("fresh-task", self.task_manager._tasks)
        self.assertNotIn("expired-task", self.task_manager._tasks)


class YesCaptchaCompatHttpTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"FCS_CLUSTER_ROLE": "standalone"}, clear=False)
        self.env_patcher.start()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "yescaptcha-http.db"
        self.db = Database(self.db_path)
        asyncio.run(self.db.init_db())
        set_database(self.db)
        self.runtime = FakeRuntime()
        self.task_manager = YesCaptchaTaskManager(task_ttl_seconds=300, cleanup_interval_seconds=30)
        asyncio.run(self.task_manager.start())

        self.app = FastAPI()
        yescaptcha.set_dependencies(self.db, self.runtime, None, self.task_manager)
        self.app.include_router(yescaptcha.router)
        self.client = TestClient(self.app)

        self.raw_key, self.api_key = asyncio.run(self.db.create_api_key("yes-http", 3))

    def tearDown(self):
        self.client.close()
        asyncio.run(self.task_manager.close())
        asyncio.run(self.db.close())
        self.temp_dir.cleanup()
        self.env_patcher.stop()

    def test_get_balance_returns_remaining_quota(self):
        response = self.client.post("/getBalance", json={"clientKey": self.raw_key})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["errorId"], 0)
        self.assertEqual(payload["balance"], 3.0)

    def test_invalid_client_key_keeps_yescaptcha_error_shape(self):
        response = self.client.post("/getBalance", json={"clientKey": "bad-key"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["errorId"], 1)
        self.assertEqual(payload["errorCode"], "ERROR_KEY_DOES_NOT_EXIST")

    def test_get_balance_reuses_short_ttl_client_key_cache(self):
        original_resolver = yescaptcha.resolve_service_api_key_token
        resolver = AsyncMock(side_effect=original_resolver)

        with patch.object(yescaptcha, "resolve_service_api_key_token", resolver):
            first = self.client.post("/getBalance", json={"clientKey": self.raw_key})
            second = self.client.post("/getBalance", json={"clientKey": self.raw_key})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["errorId"], 0)
        self.assertEqual(second.json()["errorId"], 0)
        self.assertEqual(resolver.await_count, 1)


if __name__ == "__main__":
    unittest.main()
