import unittest
from datetime import timezone

from autotest.engine import tz


class TzTests(unittest.TestCase):
    def test_now_is_beijing_offset(self):
        now = tz.now()
        offset = now.utcoffset()
        self.assertEqual(offset.total_seconds(), 8 * 3600)

    def test_from_ts_matches_known_utc_instant(self):
        # 2024-01-01 00:00:00 UTC -> 2024-01-01 08:00:00 北京时间
        ts = 1704067200.0
        dt = tz.from_ts(ts)
        self.assertEqual(dt.astimezone(timezone.utc).hour, 0)
        self.assertEqual(dt.hour, 8)


if __name__ == "__main__":
    unittest.main()
