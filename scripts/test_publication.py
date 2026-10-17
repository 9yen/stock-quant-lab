"""Publication tooling tests, run separately from installed runtime tests."""

import unittest

from check_public import findings


class PublicationTests(unittest.TestCase):
    def test_common_private_data_is_blocked_without_returning_values(self):
        samples = [
            (".env", b""),
            ("state/ledger.json", b"{}"),
            ("example.py", ("C:" + "/Users/" + "synthetic-person/private").encode()),
            ("example.py", ("ALPACA_SECRET=" + "x" * 30).encode()),
            ("example.py", ("account_id=" + "123" * 3).encode()),
        ]
        for name, raw in samples:
            report = list(findings(name, raw))
            self.assertTrue(report)
            self.assertTrue(all(len(row) == 3 for row in report))
            if raw:
                self.assertNotIn(raw.decode(), str(report))

    def test_empty_template_and_dynamic_credentials_are_allowed(self):
        self.assertEqual(list(findings(".env.example", b"ALPACA_KEY=\nALPACA_SECRET=")), [])
        self.assertEqual(list(findings("example.py", b'key = os.environ["ALPACA_KEY"]')), [])
