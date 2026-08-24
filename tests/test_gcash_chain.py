import unittest
from unittest.mock import patch

import gcash_chain


def checkout_response():
    return {
        "checkout_session_id": "oaics_fixture",
        "processor_entity": "openai_llc",
        "checkout_session": {
            "amount_total": 0,
            "custom_payment_methods": [
                {"id": "cpmt_fixture", "options": {"type": "static"}},
            ],
        },
    }


class GCashChainProtocolTests(unittest.TestCase):
    def test_registration_session_device_and_profile_are_reused(self):
        chain = gcash_chain.GCashChain(
            token="fixture-token",
            client_account_id="fixture-client",
            session_token="fixture-session-token",
            device_id="registration-device-id",
            browser_profile="chrome136",
        )
        try:
            cookies = chain._session.cookies.get_dict()
            self.assertEqual("registration-device-id", chain.device_id)
            self.assertEqual("chrome136", chain.tls_impersonate)
            self.assertEqual("registration-device-id", cookies["oai-did"])
            self.assertEqual(
                "fixture-session-token",
                cookies["__Secure-next-auth.session-token"],
            )
        finally:
            chain._session.close()

    def test_browser_aligned_protocol_order_and_headers(self):
        requests = []
        sentinel_calls = []
        chain = gcash_chain.GCashChain(
            token="fixture-token",
            client_account_id="fixture-client",
            account_id="fixture-account",
            billing_email="holder@example.com",
            billing_name="Fixture Holder",
        )

        def fake_sentinel(**kwargs):
            sentinel_calls.append(kwargs)
            return '{"flow":"' + kwargs["flow"] + '"}', ""

        def fake_request(method, url, headers, data=None, **kwargs):
            requests.append({
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": data,
            })
            if url == gcash_chain.CHECKOUT_BASE:
                if sum(item["url"] == gcash_chain.CHECKOUT_BASE for item in requests) == 1:
                    raise RuntimeError("HTTP 400 unusual activity")
                return checkout_response(), {}
            if url.endswith("/taxes"):
                return checkout_response(), {}
            if url.endswith("/confirm"):
                return {
                    "status": "success",
                    "confirm_return_url": "https://chatgpt.com/checkout/verify",
                }, {}
            if url.endswith("/custom_payment_method/start"):
                return {
                    "status": "requires_action",
                    "next_action": {
                        "paymentMethodType": "gcash",
                        "method": "GET",
                        "type": "redirect",
                        "url": (
                            "https://checkoutshopper-live.adyen.com/"
                            "checkoutshopper/checkoutPaymentRedirect"
                            "?redirectData=fixture"
                        ),
                    },
                }, {}
            raise AssertionError(f"unexpected request: {method} {url}")

        def fake_follow_redirect():
            chain.gcash_url = (
                "https://m.gcash.com/gcash-login-web/index.html"
                "?netAuthId=fixture"
            )
            chain.callback_status = "waiting_scan"

        with (
            patch.object(chain, "_preflight_proxy", return_value={"country": "PH", "ip": ""}),
            patch.object(chain, "_verify_proxy_stability"),
            patch.object(chain, "_prepare_frontend_context"),
            patch.object(chain, "_bootstrap"),
            patch.object(chain, "_follow_redirect", side_effect=fake_follow_redirect),
            patch.object(gcash_chain, "_request", side_effect=fake_request),
            patch.object(gcash_chain, "mint_sentinel_sync", side_effect=fake_sentinel),
        ):
            result = chain.run()

        self.assertEqual("success", result["status"])
        self.assertEqual("", result["error_message"])
        self.assertEqual(
            [
                gcash_chain.CHECKOUT_BASE,
                gcash_chain.CHECKOUT_BASE,
                f"{gcash_chain.CHECKOUT_BASE}/taxes",
                f"{gcash_chain.CHECKOUT_BASE}/confirm",
                f"{gcash_chain.CHECKOUT_BASE}/custom_payment_method/start",
            ],
            [request["url"] for request in requests],
        )
        self.assertFalse(any(request["url"].endswith("/update") for request in requests))
        self.assertEqual(
            ["chatgpt_checkout", "checkout_session_approval"],
            [call["flow"] for call in sentinel_calls],
        )

        first_create, create, taxes, confirm, start = requests
        self.assertNotIn("OpenAI-Sentinel-Token", first_create["headers"])
        self.assertEqual("fixture-account", create["headers"]["ChatGPT-Account-Id"])
        self.assertEqual(
            '{"flow":"chatgpt_checkout"}',
            create["headers"]["OpenAI-Sentinel-Token"],
        )
        self.assertEqual("plus-1-month-free", create["body"]["promo_campaign"]["promo_campaign_id"])
        self.assertTrue(create["body"]["promo_campaign"]["is_coupon_from_query_param"])
        self.assertTrue(create["body"]["check_card_proxy"])
        self.assertEqual({"country": "PH", "currency": "PHP"}, create["body"]["billing_details"])
        self.assertEqual(
            "https://chatgpt.com/?promo_campaign=plus-1-month-free",
            create["headers"]["Referer"],
        )

        self.assertNotIn("OpenAI-Sentinel-Token", taxes["headers"])
        self.assertEqual("holder@example.com", taxes["body"]["checkout_email"])
        self.assertEqual("Fixture Holder", taxes["body"]["billing_name"])
        self.assertEqual({"country": "PH"}, taxes["body"]["billing_address"])
        self.assertIsNone(taxes["body"]["tax_id"])

        self.assertEqual("fixture-account", confirm["headers"]["ChatGPT-Account-Id"])
        self.assertEqual(
            '{"flow":"checkout_session_approval"}',
            confirm["headers"]["OpenAI-Sentinel-Token"],
        )
        self.assertEqual("cpmt_fixture", confirm["body"]["selected_payment_method_type"])
        self.assertNotIn("OpenAI-Sentinel-Token", start["headers"])
        self.assertEqual("adyen_redirect", chain.payment_route)
        self.assertEqual(chain.device_id, sentinel_calls[0]["device_id"])
        self.assertEqual(chain.device_id, sentinel_calls[1]["device_id"])
        self.assertEqual(
            "https://chatgpt.com/checkout/openai_llc/oaics_fixture",
            sentinel_calls[1]["page_url"],
        )

    def test_adyen_redirect_requires_exact_host_path_and_redirect_data(self):
        valid = (
            "https://checkoutshopper-live.adyen.com/"
            "checkoutshopper/checkoutPaymentRedirect?redirectData=fixture"
        )
        self.assertTrue(gcash_chain._is_adyen_checkout_redirect(valid))
        self.assertFalse(gcash_chain._is_adyen_checkout_redirect(valid.replace("redirectData=fixture", "x=1")))
        self.assertFalse(gcash_chain._is_adyen_checkout_redirect(valid.replace("checkoutshopper-live", "evil")))

    def test_create_does_not_mint_sentinel_for_non_risk_failure(self):
        chain = gcash_chain.GCashChain(
            token="fixture-token",
            client_account_id="fixture-client",
            proxy="proxy.example:8080:user:pass",
        )
        with (
            patch.object(chain, "_prepare_frontend_context"),
            patch.object(chain, "_bootstrap"),
            patch.object(chain, "_verify_proxy_stability"),
            patch.object(
                gcash_chain,
                "_request",
                side_effect=RuntimeError("HTTP 403 Cloudflare"),
            ),
            patch.object(gcash_chain, "mint_sentinel_sync") as mint,
        ):
            with self.assertRaisesRegex(RuntimeError, "403"):
                chain._create_checkout()
        mint.assert_not_called()

    def test_outer_retry_keeps_registration_context_for_network_errors(self):
        manager = gcash_chain.GCashSessionManager(max_concurrency=1, max_queue=1)
        task = {
            "token": "fixture-token",
            "session_token": "fixture-session",
            "device_id": "registration-device",
            "browser_profile": "chrome136",
            "client_account_id": "fixture-client",
            "account_id": "fixture-account",
            "billing_email": "holder@example.com",
            "billing_name": "Holder",
            "proxy": "proxy-1.example:8080",
            "proxy_pool": ["proxy-1.example:8080", "proxy-2.example:8080"],
            "max_attempts": 2,
            "status": "running",
            "current_step": "init",
            "steps": [],
            "error_message": "",
            "gcash_url": "",
            "expires_at": None,
            "monitor_id": "",
            "callback_status": "unavailable",
            "payment_route": "",
            "attempts_used": 0,
            "attempt_history": [],
            "cancel_requested": False,
        }
        constructor_calls = []

        class FakeChain:
            def __init__(self, **kwargs):
                constructor_calls.append(kwargs)

            def run(self):
                failed = len(constructor_calls) == 1
                return {
                    "status": "failed" if failed else "success",
                    "current_step": "create_checkout" if failed else "follow_redirect",
                    "steps": [],
                    "error_message": "connection timeout" if failed else "",
                    "gcash_url": "https://m.gcash.com/gcash-login-web/index.html?netAuthId=test" if not failed else "",
                    "qr_expires_at": None,
                    "monitor_id": "",
                    "callback_status": "unavailable",
                    "payment_route": "",
                }

        try:
            with patch.object(gcash_chain, "GCashChain", FakeChain):
                manager._process_task(task)
        finally:
            manager.executor.shutdown(wait=True)

        self.assertEqual(2, len(constructor_calls))
        self.assertEqual(
            ["registration-device", "registration-device"],
            [call["device_id"] for call in constructor_calls],
        )
        self.assertEqual(
            ["fixture-session", "fixture-session"],
            [call["session_token"] for call in constructor_calls],
        )
        self.assertNotIn("proxy", task["attempt_history"][0])
        self.assertEqual(
            gcash_chain._proxy_ref("proxy-1.example:8080"),
            task["attempt_history"][0]["proxy_ref"],
        )

    def test_unusual_activity_stops_after_same_session_sentinel_retry(self):
        result = {
            "status": "failed",
            "current_step": "create_checkout",
            "error_message": "Our systems have detected unusual activity",
        }
        self.assertEqual((False, ""), gcash_chain._retry_decision(result))


if __name__ == "__main__":
    unittest.main()
