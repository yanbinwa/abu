"""Alpha158 canonical family-wise audit tests."""
import unittest

import numpy as np

from scripts.audit_alpha158_canonical_familywise_v2 import (
    centered_block_p_positive,
)


class Alpha158FamilywiseAuditTest(unittest.TestCase):

    def test_centered_block_test_is_deterministic(self):
        values = np.linspace(-.01, .03, 200)
        first = centered_block_p_positive(
            values, block_length=20, replicates=500, seed=7)
        second = centered_block_p_positive(
            values, block_length=20, replicates=500, seed=7)
        self.assertEqual(first, second)
        self.assertLess(first, .10)

    def test_nonpositive_signal_does_not_pass_positive_test(self):
        values = np.linspace(-.03, .01, 200)
        result = centered_block_p_positive(
            values, block_length=20, replicates=500, seed=11)
        self.assertGreater(result, .50)


if __name__ == "__main__":
    unittest.main()
