"""Weighted mailbox domain selection.

``defaultDomains`` accepts a ``^N`` suffix so one domain can be preferred
several times over another. The schedule is deterministic rather than sampled:
for ``a.xyz^3, b.xyz`` every window of four picks must contain three ``a.xyz``,
not converge on that ratio only on average.
"""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.mailbox import cloud_mail
from backend.mailbox.utilities import parse_weighted_domains, weighted_domain_schedule


class WeightedDomainParsingTests(unittest.TestCase):
    def test_plain_list_has_unit_weights(self):
        self.assertEqual(
            parse_weighted_domains("a.xyz, b.xyz"),
            [("a.xyz", 1), ("b.xyz", 1)],
        )

    def test_weight_suffix_is_parsed(self):
        self.assertEqual(
            parse_weighted_domains("123.xyz^3, 234.xyz^1"),
            [("123.xyz", 3), ("234.xyz", 1)],
        )

    def test_bare_domain_implies_weight_one(self):
        self.assertEqual(parse_weighted_domains("a.xyz^3, b.xyz"), [("a.xyz", 3), ("b.xyz", 1)])

    def test_space_separated_is_accepted(self):
        self.assertEqual(
            parse_weighted_domains("a.xyz b.xyz"),
            [("a.xyz", 1), ("b.xyz", 1)],
        )

    def test_chinese_comma_is_accepted(self):
        self.assertEqual(
            parse_weighted_domains("a.xyz，b.xyz"),
            [("a.xyz", 1), ("b.xyz", 1)],
        )

    def test_whitespace_around_caret_is_tolerated(self):
        self.assertEqual(parse_weighted_domains("a.xyz ^ 3"), [("a.xyz", 3)])

    def test_zero_and_negative_weights_fall_back_to_one(self):
        self.assertEqual(parse_weighted_domains("a.xyz^0"), [("a.xyz", 1)])

    def test_absurd_weight_is_clamped(self):
        self.assertEqual(parse_weighted_domains("a.xyz^99999")[0][1], 1000)

    def test_empty_input_yields_nothing(self):
        self.assertEqual(parse_weighted_domains(""), [])
        self.assertEqual(parse_weighted_domains("   "), [])


class WeightedDomainScheduleTests(unittest.TestCase):
    def test_schedule_expands_by_weight(self):
        self.assertEqual(
            weighted_domain_schedule("123.xyz^3, 234.xyz^1"),
            ["123.xyz", "123.xyz", "123.xyz", "234.xyz"],
        )

    def test_duplicates_sum_their_weights(self):
        self.assertEqual(
            Counter(weighted_domain_schedule("a.xyz^2, a.xyz^1, b.xyz")),
            Counter({"a.xyz": 3, "b.xyz": 1}),
        )

    def test_unweighted_list_stays_uniform(self):
        schedule = weighted_domain_schedule("a.xyz, b.xyz, c.xyz")
        self.assertEqual(len(schedule), 3)
        self.assertEqual(set(schedule), {"a.xyz", "b.xyz", "c.xyz"})


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200
        self.text = str(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class CloudMailDomainRotationTests(unittest.TestCase):
    """create_mailbox must walk the weighted schedule, not a plain round-robin."""

    def setUp(self):
        cloud_mail.reset_runtime_state()
        self.addCleanup(cloud_mail.reset_runtime_state)

    @staticmethod
    def _capture(domains: list[str], count: int) -> list[str]:
        created: list[str] = []

        def fake_post(url, json=None, **kwargs):
            # add_address posts {"email": address, "token": ""}; the login
            # call posts credentials instead and must not be counted.
            body = json if isinstance(json, dict) else {}
            address = body.get("email", "")
            if address and "token" in body:
                created.append(address)
            return _FakeResponse({"code": 200, "data": {"accountId": 1, "token": "jwt"}})

        for _ in range(count):
            cloud_mail.create_mailbox(
                http_post=fake_post,
                url="https://mail.example",
                admin_email="admin@example",
                admin_password="pw",
                domains=domains,
            )
        return [item.split("@")[1] for item in created]

    def test_four_registrations_split_three_to_one(self):
        used = self._capture(["123.xyz^3", "234.xyz^1"], 4)
        self.assertEqual(Counter(used), Counter({"123.xyz": 3, "234.xyz": 1}))
        self.assertEqual(used[0], "123.xyz")

    def test_weights_hold_across_multiple_windows(self):
        used = self._capture(["123.xyz^3", "234.xyz^1"], 12)
        self.assertEqual(Counter(used), Counter({"123.xyz": 9, "234.xyz": 3}))

    def test_plain_list_still_round_robins(self):
        used = self._capture(["a.xyz", "b.xyz"], 4)
        self.assertEqual(Counter(used), Counter({"a.xyz": 2, "b.xyz": 2}))

    def test_empty_domains_raises(self):
        with self.assertRaises(Exception):
            cloud_mail.create_mailbox(
                http_post=lambda *a, **k: {},
                url="https://mail.example",
                admin_email="admin@example",
                admin_password="pw",
                domains=["^3"],
            )


if __name__ == "__main__":
    unittest.main()