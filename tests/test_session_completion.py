import unittest
from datetime import datetime, timedelta, timezone

from quant_lab.dashboard.data import session_completed


class SessionCompletionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        final = dict(status="FLAT", done=True, flat=True)
        self.local = dict(alive=False, last=final, events=[final])
        self.account = dict(fetched=self.now.isoformat(), holdings=[], open_symbols=[])

    def test_confirmed_normal_completion(self):
        self.assertTrue(session_completed(self.local, self.account, now=self.now))

    def test_crash_or_later_event_is_not_completion(self):
        self.local["last"]["done"] = False
        self.assertFalse(session_completed(self.local, self.account, now=self.now))
        self.local["last"]["done"] = True
        self.local["events"].append(dict(event="ERROR"))
        self.assertFalse(session_completed(self.local, self.account, now=self.now))

    def test_positions_pending_orders_and_unavailable_account_still_alert(self):
        for field in ("holdings", "open_symbols"):
            account = dict(self.account, **{field: ["SPY"]})
            self.assertFalse(session_completed(self.local, account, now=self.now))
        self.assertFalse(session_completed(self.local, self.account, "Timeout", self.now))
        self.assertFalse(session_completed(self.local, {}, now=self.now))

    def test_stale_or_future_snapshot_cannot_hide_process_warning(self):
        for seconds in (-46, 10):
            self.account["fetched"] = (self.now + timedelta(seconds=seconds)).isoformat()
            self.assertFalse(session_completed(self.local, self.account, now=self.now))

    def test_running_process_is_not_reported_completed(self):
        self.local["alive"] = True
        self.assertFalse(session_completed(self.local, self.account, now=self.now))
