import time
import unittest
from unittest.mock import patch

from src.services.browser_captcha import BrowserCaptchaService, StandbyTokenEntry


class BrowserTokenPoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()
        self.bucket_key = "project-a|IMAGE_GENERATION|-"

    async def test_expired_entry_does_not_hit(self):
        now_value = time.monotonic()
        self.service._standby_tokens[self.bucket_key] = [
            StandbyTokenEntry(
                token="expired-token",
                browser_id=1,
                fingerprint={"user_agent": "ua-expired"},
                browser_epoch=3,
                project_id="project-a",
                action="IMAGE_GENERATION",
                proxy_signature="-",
                created_monotonic=now_value - 10,
                expires_monotonic=now_value - 1,
            )
        ]

        with patch.object(self.service, "_get_browser_epoch_for_standby", return_value=3):
            result = await self.service._take_standby_token(self.bucket_key)

        self.assertIsNone(result)
        self.assertNotIn(self.bucket_key, self.service._standby_tokens)

    async def test_hit_pops_entry_from_pool(self):
        now_value = time.monotonic()
        self.service._standby_tokens[self.bucket_key] = [
            StandbyTokenEntry(
                token="warm-token",
                browser_id=2,
                fingerprint={"user_agent": "ua-live"},
                browser_epoch=5,
                project_id="project-a",
                action="IMAGE_GENERATION",
                proxy_signature="-",
                created_monotonic=now_value,
                expires_monotonic=now_value + 30,
            )
        ]

        with patch.object(self.service, "_get_browser_epoch_for_standby", return_value=5):
            result = await self.service._take_standby_token(self.bucket_key)

        self.assertIsNotNone(result)
        self.assertEqual(result.token, "warm-token")
        self.assertEqual(result.browser_ref, 2)
        self.assertEqual(result.browser_epoch, 5)
        self.assertEqual(result.fingerprint, {"user_agent": "ua-live"})
        self.assertNotIn(self.bucket_key, self.service._standby_tokens)

    async def test_epoch_mismatch_invalidates_entry(self):
        now_value = time.monotonic()
        self.service._standby_tokens[self.bucket_key] = [
            StandbyTokenEntry(
                token="stale-token",
                browser_id=4,
                fingerprint={"user_agent": "ua-stale"},
                browser_epoch=7,
                project_id="project-a",
                action="IMAGE_GENERATION",
                proxy_signature="-",
                created_monotonic=now_value,
                expires_monotonic=now_value + 30,
            )
        ]

        with patch.object(self.service, "_get_browser_epoch_for_standby", return_value=8):
            result = await self.service._take_standby_token(self.bucket_key)

        self.assertIsNone(result)
        self.assertNotIn(self.bucket_key, self.service._standby_tokens)


if __name__ == "__main__":
    unittest.main()
