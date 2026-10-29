"""Regression checks for ingestion SLA and accuracy gates."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from accuracy_label_plan import build_plan
from false_flag_calibration import calibration_report
from ingest_scorecard import (
    aggregate_summaries,
    load_performance_evidence,
    target_failures,
    wilson_lower_bound,
    write_release_verdict,
)


def _summary(*, accuracy=0.96, false_pass=0.01, total=200.0, fields=50):
    return {
        "gt_fields": fields,
        "rates": {"accuracy": accuracy, "false_pass_rate": false_pass},
        "timings_s": {"total": total},
    }


class TargetFailuresTest(unittest.TestCase):
    def test_passing_targets(self):
        self.assertEqual(
            target_failures(
                _summary(),
                min_accuracy=0.95,
                max_total_seconds=240,
                max_false_pass_rate=0.02,
                min_ground_truth_fields=30,
            ),
            [],
        )

    def test_all_target_failures_are_reported(self):
        failures = target_failures(
            _summary(accuracy=0.9, false_pass=0.08, total=260, fields=10),
            min_accuracy=0.95,
            max_total_seconds=240,
            max_false_pass_rate=0.02,
            min_ground_truth_fields=30,
        )
        self.assertEqual(len(failures), 4)

    def test_small_perfect_sample_does_not_prove_production_accuracy(self):
        self.assertLess(wilson_lower_bound(21, 21), 0.95)

    def test_label_only_zero_time_does_not_prove_performance(self):
        failures = target_failures(
            _summary(total=0),
            min_accuracy=None,
            max_total_seconds=240,
            max_false_pass_rate=None,
            min_ground_truth_fields=1,
        )
        self.assertEqual(failures, ["ingestion performance evidence missing"])

    def test_label_summaries_are_gated_as_one_corpus(self):
        combined = aggregate_summaries(
            [
                _summary(accuracy=1.0, false_pass=0.0, fields=100)
                | {"correct_total": 100, "categories": {"correct_verified": 100}}
                for _ in range(2)
            ]
        )
        self.assertEqual(combined["gt_fields"], 200)
        self.assertGreater(combined["rates"]["accuracy_confidence_95_lower"], 0.95)

    def test_release_coverage_and_false_flags_are_enforced(self):
        summary = _summary(fields=100) | {
            "deals": 1,
            "concept_families": {"economics": {"fields": 100, "correct": 96}},
            "rates": {
                "accuracy": 0.96,
                "accuracy_confidence_95_lower": 0.91,
                "false_pass_rate": 0.0,
                "false_flag_rate": 0.8,
            },
        }
        failures = target_failures(
            summary,
            min_accuracy=0.95,
            max_total_seconds=240,
            max_false_pass_rate=0.01,
            min_ground_truth_fields=73,
            min_accuracy_confidence_lower=0.95,
            max_false_flag_rate=0.2,
            min_deals=3,
            min_concept_families=5,
        )
        self.assertEqual(len(failures), 4)

    def test_release_verdict_is_machine_readable_and_contains_no_fact_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_release_verdict(Path(tmp), _summary(), ["not ready"])
            payload = json.loads((Path(tmp) / "release-verdict.json").read_text())
        self.assertFalse(payload["passed"])
        self.assertEqual(payload["failures"], ["not ready"])
        self.assertNotIn("facts", payload)

    def test_performance_evidence_requires_a_representative_document_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scorecard.json"
            path.write_text(json.dumps({"summary": {"timings_s": {"total": 200}, "docs": {"pdf": 49}}}))
            with self.assertRaisesRegex(ValueError, "at least 50"):
                load_performance_evidence(path)
            path.write_text(json.dumps({"summary": {"timings_s": {"total": 200}, "docs": {"pdf": 178}}}))
            evidence = load_performance_evidence(path)
        self.assertEqual(evidence["documents"], 178)
        self.assertEqual(len(evidence["sha256"]), 64)

    def test_label_plan_reports_coverage_without_values(self):
        labels = [
            SimpleNamespace(deal_id=f"deal-{index % 3}", concept_path=f"family{index % 5}.field{index}")
            for index in range(75)
        ]
        plan = build_plan(labels)
        self.assertTrue(plan["ready_for_scoring"])
        self.assertEqual(plan["deficits"], {"labels": 0, "deals": 0, "concept_families": 0})
        self.assertNotIn("values", json.dumps(plan))

    def test_unclassified_false_flags_fail_release_observability_gate(self):
        summary = _summary(fields=100) | {
            "deals": 3,
            "concept_families": {f"f{i}": {"fields": 20, "correct": 20} for i in range(5)},
            "rates": {
                "accuracy": 1.0,
                "accuracy_confidence_95_lower": 0.963,
                "false_pass_rate": 0.0,
                "false_flag_rate": 0.1,
                "unclassified_false_flag_rate": 0.5,
            },
        }
        failures = target_failures(
            summary,
            min_accuracy=0.95,
            max_total_seconds=240,
            max_false_pass_rate=0.01,
            min_ground_truth_fields=73,
            min_accuracy_confidence_lower=0.95,
            max_false_flag_rate=0.2,
            max_unclassified_false_flag_rate=0.05,
            min_deals=3,
            min_concept_families=5,
        )
        self.assertEqual(failures, ["unclassified false-flag rate 0.5 > 0.05"])

    def test_calibration_is_evidence_only_and_protected_reasons_never_relax(self):
        def label(index, reason):
            return SimpleNamespace(
                source="analyst",
                deal_id=f"deal-{index % 3}",
                concept_path=f"economics.field{index}",
                passed=False,
                verify_reasons=(reason,),
                extracted_value="100",
                final_value="100",
            )

        labels = [label(index, "LOW_CONFIDENCE") for index in range(73)]
        labels += [label(index + 100, "CONTRADICTION") for index in range(73)]
        report = calibration_report(labels)
        self.assertEqual(
            report["shadow_candidates"],
            [{"family": "economics", "reason": "LOW_CONFIDENCE"}],
        )
        self.assertEqual(report["runtime_changes"], [])
        self.assertNotIn("100", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
