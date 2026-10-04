import tempfile
import unittest
from pathlib import Path

from abupy.AlphaBu.ABuIntradayAdmission import (
    AdmissionEvidence, evaluate_admission, write_admission_report,
)


def evidence(**changes):
    values = dict(
        software_milestones_passed=True,
        full_regression_passed=True,
        d0_parity_passed=True,
        causal_tests_passed=True,
        paired_replay_completed=True,
        portfolio_backtest_completed=True,
        calibration_effective_days=0,
        thresholds_frozen=False,
        qualification_effective_days=0,
        shadow_quality_passed=False,
        unresolved_critical_defects=0,
    )
    values.update(changes)
    return AdmissionEvidence(**values)


class IntradayAdmissionTest(unittest.TestCase):

    def test_software_only_can_accept_data_but_not_execution(self):
        report = evaluate_admission(evidence())
        self.assertEqual("ACCEPT_DATA_ONLY", report["decision"])
        self.assertFalse(report["minute_sell_research_allowed"])

    def test_failed_quality_repeats_and_all_gates_accept(self):
        calibrated = dict(
            calibration_effective_days=10, thresholds_frozen=True,
            qualification_effective_days=20)
        failed = evaluate_admission(evidence(**calibrated))
        passed = evaluate_admission(evidence(
            **calibrated, shadow_quality_passed=True))
        self.assertEqual("REVISE_AND_REPEAT", failed["decision"])
        self.assertEqual("ACCEPT_V1", passed["decision"])
        self.assertTrue(passed["minute_sell_research_allowed"])

    def test_critical_defect_rejects_and_report_is_immutable(self):
        report = evaluate_admission(evidence(unresolved_critical_defects=1))
        self.assertEqual("REJECT", report["decision"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admission.json"
            write_admission_report(path, report)
            with self.assertRaises(FileExistsError):
                write_admission_report(path, report)


if __name__ == "__main__":
    unittest.main()
