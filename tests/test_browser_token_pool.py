import time
import unittest
from unittest.mock import AsyncMock, patch

from src.services.browser_captcha import BrowserCaptchaService, StandbyTokenEntry, TokenBrowser


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

    async def test_custom_token_uses_shared_browser_path(self):
        browser = TokenBrowser(3, "tmp/test-custom-token-shared")
        fake_context = object()

        with patch.object(
            browser,
            "_get_or_create_shared_browser",
            AsyncMock(return_value=(object(), object(), fake_context)),
        ) as shared_browser_mock:
            with patch.object(
                browser,
                "_create_browser",
                AsyncMock(side_effect=AssertionError("should not create temporary browser")),
            ):
                with patch.object(
                    browser,
                    "_execute_custom_captcha",
                    AsyncMock(return_value="shared-custom-token"),
                ) as execute_mock:
                    token = await browser.get_custom_token(
                        website_url="https://example.com/login",
                        website_key="site-key",
                        action="login",
                    )

        self.assertEqual(token, "shared-custom-token")
        shared_browser_mock.assert_awaited_once()
        execute_mock.assert_awaited_once()
        self.assertTrue(bool(execute_mock.await_args.kwargs["reuse_ready_page"]))

    async def test_custom_page_cache_hits_same_site(self):
        browser = TokenBrowser(4, "tmp/test-custom-page-cache")
        website_url = "https://example.com/login"
        website_key = "site-key"
        custom_key = browser._build_custom_page_key(
            website_url=website_url,
            website_key=website_key,
            captcha_type="recaptcha_v3",
            enterprise=False,
        )

        class FakePage:
            def is_closed(self):
                return False

            async def evaluate(self, _expression):
                return True

        fake_page = FakePage()
        browser._shared_custom_pages[custom_key] = fake_page
        browser._shared_custom_page_last_used[custom_key] = 1.0

        class FakeContext:
            async def new_page(self):
                raise AssertionError("cache hit should not create a new page")

        page, resolved_key, runtime, ready_hit = await browser._get_or_create_custom_page(
            FakeContext(),
            website_url=website_url,
            website_key=website_key,
            captcha_type="recaptcha_v3",
            enterprise=False,
        )

        self.assertIs(page, fake_page)
        self.assertEqual(resolved_key, custom_key)
        self.assertEqual(runtime["normalized_type"], "recaptcha_v3")
        self.assertTrue(ready_hit)

    async def test_service_custom_token_uses_site_affinity_slot_selection(self):
        service = BrowserCaptchaService()

        class FakeBrowser:
            def __init__(self):
                self.get_custom_token = AsyncMock(return_value="service-token")

        fake_browser = FakeBrowser()

        with patch.object(service, "_check_available"):
            with patch.object(service, "_resolve_global_proxy_url", AsyncMock(return_value=None)):
                with patch.object(service, "_select_browser_id", AsyncMock(return_value=2)) as select_mock:
                    with patch.object(service, "_get_next_browser_id", side_effect=AssertionError("should not use round robin")):
                        with patch.object(service, "_get_or_create_browser", AsyncMock(return_value=fake_browser)):
                            token, browser_id = await service.get_custom_token(
                                website_url="https://example.com/login",
                                website_key="site-key",
                                action="login",
                                captcha_type="recaptcha_v3",
                            )

        self.assertEqual(token, "service-token")
        self.assertEqual(browser_id, 2)
        select_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
