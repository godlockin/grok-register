import unittest
from unittest import mock

from backend.mailbox import temp_mail_io
from backend.registration import engine as gr
from backend.integrations import network_checks


class _MockResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or str(json_data)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}: {self.text}")

    def json(self):
        return self._json_data


class TempMailIoTests(unittest.TestCase):
    def test_get_domains_and_pick_domain(self):
        def mock_get(url, **kwargs):
            return _MockResponse(
                200,
                {
                    "domains": [
                        {"name": "ozsaip.com", "type": "public"},
                        {"name": "yzcalo.com", "type": "public"},
                    ]
                },
            )

        domains = temp_mail_io.get_domains(mock_get)
        self.assertEqual(domains, ["ozsaip.com", "yzcalo.com"])

        picked = temp_mail_io.pick_domain(domains, preferred_domain="yzcalo.com")
        self.assertEqual(picked, "yzcalo.com")

        random_picked = temp_mail_io.pick_domain(domains)
        self.assertIn(random_picked, domains)

    def test_create_mailbox(self):
        def mock_get(url, **kwargs):
            return _MockResponse(200, {"domains": [{"name": "ozsaip.com"}]})

        def mock_post(url, **kwargs):
            payload = kwargs.get("json", {})
            self.assertIn("name", payload)
            self.assertEqual(payload["domain"], "ozsaip.com")
            return _MockResponse(
                200,
                {"email": f"{payload['name']}@ozsaip.com", "token": "token123"},
            )

        email, token = temp_mail_io.create_mailbox(
            mock_get, mock_post, preferred_domain="ozsaip.com", username="testuser"
        )
        self.assertEqual(email, "testuser@ozsaip.com")
        self.assertEqual(token, "token123")

    def test_wait_for_code_extracts_code(self):
        def mock_get(url, **kwargs):
            if "messages" in url:
                return _MockResponse(
                    200,
                    [
                        {
                            "id": "msg-1",
                            "subject": "Your verification code: I6R-B2W",
                            "body_text": "Please use this code: I6R-B2W to sign up.",
                            "body_html": "<p>Code: I6R-B2W</p>",
                        }
                    ],
                )
            return _MockResponse(404)

        code = temp_mail_io.wait_for_code(
            mock_get,
            "https://api.internal.temp-mail.io/api/v3",
            "testuser@ozsaip.com",
            token="token123",
            timeout=5,
            raise_if_cancelled=lambda cb: None,
            sleep_with_cancel=lambda s, cb: None,
        )
        self.assertEqual(code, "I6R-B2W")

    def test_delete_mailbox(self):
        deleted = False

        def mock_delete(url, **kwargs):
            nonlocal deleted
            deleted = True
            self.assertEqual(kwargs.get("json"), {"token": "token123"})
            return _MockResponse(200, {})

        res = temp_mail_io.delete_mailbox(
            mock_delete,
            "https://api.internal.temp-mail.io/api/v3",
            "testuser@ozsaip.com",
            "token123",
        )
        self.assertTrue(res)
        self.assertTrue(deleted)

    def test_network_check_email_api_tempmail_io(self):
        def mock_get(url, **kwargs):
            return _MockResponse(200, {"domains": [{"name": "ozsaip.com"}]})

        name, ok, detail = network_checks.check_email_api(
            "tempmail_io", {}, mock_get, None
        )
        self.assertEqual(name, "邮箱API")
        self.assertTrue(ok)
        self.assertIn("TempMail.io 可达", detail)


if __name__ == "__main__":
    unittest.main()
