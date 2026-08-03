import unittest

from autotest.engine.batch import _partition_targets


class BrowserPoolPartitionTests(unittest.TestCase):
    def test_single_worker_reuses_one_group_for_all_pages(self):
        targets = [("A", "a.yaml"), ("B", "b.yaml"), ("C", "c.yaml")]
        groups = _partition_targets(targets, 1)
        self.assertEqual([[
            (0, "A", "a.yaml"),
            (1, "B", "b.yaml"),
            (2, "C", "c.yaml"),
        ]], groups)

    def test_two_workers_receive_stable_round_robin_groups(self):
        targets = [("A", "a"), ("B", "b"), ("C", "c"), ("D", "d")]
        groups = _partition_targets(targets, 2)
        self.assertEqual([(0, "A", "a"), (2, "C", "c")], groups[0])
        self.assertEqual([(1, "B", "b"), (3, "D", "d")], groups[1])

    def test_does_not_create_empty_browser_workers(self):
        self.assertEqual(1, len(_partition_targets([("A", "a")], 4)))


if __name__ == "__main__":
    unittest.main()
