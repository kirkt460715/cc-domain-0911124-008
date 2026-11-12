import unittest
from math import comb

from helpers import app, envelope, receive, T0
from chainledger.types import ChainError
from chainledger.graph import sampling_stats as stats


def hypergeo(N, D, n, x):
    return comb(D, x) * comb(N - D, n - x) / comb(N, n)


class HypergeometricTests(unittest.TestCase):
    def test_mass_matches_exact_combinatorics(self):
        N, D, n = 53, 8, 14
        mass = stats.distribution(N, D, n)
        expected = [hypergeo(N, D, n, x) for x in range(n + 1)]
        self.assertAlmostEqual(sum(mass), 1.0)
        for got, want in zip(mass, expected):
            self.assertAlmostEqual(got, want, places=12)

    def test_full_inspection_separates_defect_counts(self):
        evaluation = stats.evaluate_single(25, 25, 3)
        self.assertEqual(evaluation["curve"][3], 1.0)
        self.assertEqual(evaluation["curve"][4], 0.0)

    def test_double_plan_without_replacement(self):
        # Second stage draws from the remaining N-n1 units; brute-force it.
        N, n1, a1, r1, n2, a2, D = 100, 13, 0, 2, 13, 1, 10
        evaluation = stats.evaluate_double(N, n1, a1, r1, n2, a2)
        first = stats.distribution(N, D, n1)
        brute = first[0] + first[1] * stats._head_cdf(N - n1, D - 1, n2, 0)
        self.assertAlmostEqual(evaluation["curve"][D], brute, places=12)
        self.assertGreaterEqual(evaluation["asn_curve"][D], n1)
        self.assertLessEqual(evaluation["asn_curve"][D], n1 + n2)

    def test_average_sample_size_bounds(self):
        evaluation = stats.evaluate_double(80, 12, 0, 3, 20, 2)
        for asn in evaluation["asn_curve"]:
            self.assertGreaterEqual(asn, 12)
            self.assertLessEqual(asn, 32)


class SearchTests(unittest.TestCase):
    def test_single_plan_meets_both_risk_points(self):
        result = stats.design_sampling_plan(200, 0.01, 0.06, 0.05, 0.10, "single")
        self.assertTrue(result["feasible"])
        risks = result["risks"]
        self.assertLessEqual(risks["producer_risk_at_aql"], 0.05 + 1e-9)
        self.assertLessEqual(risks["consumer_risk_at_rql"], 0.10 + 1e-9)

    def test_infeasible_design_is_reported_not_fudged(self):
        result = stats.design_sampling_plan(
            20000, 0.0005, 0.002, 0.05, 0.10, "single"
        )
        self.assertFalse(result["feasible"])
        self.assertIn("reason", result)
        self.assertIn("limits", result["design"])

    def test_limits_and_preference_are_queryable(self):
        limits = stats.search_limits()
        self.assertEqual(
            limits["probability_model"], "hypergeometric without replacement"
        )
        self.assertIn("single_sample_cap", limits)
        self.assertTrue(limits["preference"])

    def test_rounding_collision_is_infeasible(self):
        result = stats.design_sampling_plan(10, 0.10, 0.12, 0.05, 0.05)
        self.assertFalse(result["feasible"])

    def test_any_kind_prefers_single(self):
        result = stats.design_sampling_plan(200, 0.01, 0.06, 0.05, 0.10)
        self.assertEqual(result["kind"], "single")

    def test_double_search_matches_exhaustive_scan(self):
        original_first = stats.DOUBLE_FIRST_CAP
        original_total = stats.DOUBLE_TOTAL_CAP
        stats.DOUBLE_FIRST_CAP = 12
        stats.DOUBLE_TOTAL_CAP = 24
        try:
            for N, aql, rql, alpha, beta in [
                (60, 0.02, 0.10, 0.10, 0.20),
                (80, 0.03, 0.12, 0.10, 0.20),
                (70, 0.04, 0.15, 0.10, 0.25),
            ]:
                d0 = stats.defect_count(N, aql)
                d1 = stats.defect_count(N, rql)
                engine = stats.design_sampling_plan(N, aql, rql, alpha, beta, "double")
                exhaustive = self._exhaustive(
                    N, d0, d1, 1 - alpha, beta, 12, 24
                )
                if engine["feasible"]:
                    plan = engine["plan"]
                    got = (
                        plan["first_sample_size"],
                        plan["first_accept"],
                        plan["first_reject"],
                        plan["second_sample_size"],
                        plan["cumulative_accept"],
                    )
                else:
                    got = None
                self.assertEqual(got, exhaustive, N)
        finally:
            stats.DOUBLE_FIRST_CAP = original_first
            stats.DOUBLE_TOTAL_CAP = original_total

    @staticmethod
    def _exhaustive(N, d0, d1, target, beta, fcap, tcap):
        best = None
        for n1 in range(1, min(fcap, N - 1) + 1):
            fa = stats.distribution(N, d0, n1)
            fr = stats.distribution(N, d1, n1)
            n2cap = min(tcap, N) - n1
            if n2cap < 1:
                break
            for a1 in range(n1):
                if sum(fr[: a1 + 1]) > beta + 1e-12:
                    break
                for r1 in range(a1 + 2, n1 + 2):
                    for n2 in range(1, n2cap + 1):
                        for a2 in range(a1 + 1, r1):
                            pa0 = stats._double_accept(
                                N, d0, n1, a1, r1, n2, a2, fa
                            )
                            pa1 = stats._double_accept(
                                N, d1, n1, a1, r1, n2, a2, fr
                            )
                            if pa0 + 1e-12 >= target and pa1 - 1e-12 <= beta:
                                ev = stats.evaluate_double(N, n1, a1, r1, n2, a2)
                                order = (ev["asn_curve"][d0], n1, n1 + n2, a1, a2)
                                if best is None or order < best[0]:
                                    best = (order, (n1, a1, r1, n2, a2))
            if best:
                break
        return best[1] if best else None


class AcceptanceSamplingDomainTests(unittest.TestCase):
    def setUp(self):
        self.application = app()
        self.addCleanup(self.application.close)
        self.application.consumer.submit(
            envelope(ops=[receive(quantity="500")], at=T0)
        )
        self.application.quality.specification(
            {
                "id": "sp",
                "material": "alloy",
                "minimum_samples": 5,
                "maximum_defects": 1,
                "criteria": [
                    {"name": "hardness", "unit": "HB", "lower": "10", "upper": "20"}
                ],
            }
        )
        self.plan_request = {
            "id": "P1",
            "material": "alloy",
            "lot_size": 100,
            "unit": {"kind": "piece", "label": "forged-piece"},
            "aql": "0.02",
            "rql": "0.10",
            "producer_risk": "0.05",
            "consumer_risk": "0.10",
            "kind": "double",
            "inspection_spec": "sp",
            "criteria": [{"name": "hardness"}],
        }
        self.application.sampling.approve_plan(self.plan_request)
        self.application.sampling.bind("R1", "P1", "raw", 100, "500", "kg")

    def measurement(self, value="15"):
        return {"hardness": {"value": value, "unit": "HB"}}

    def test_piece_count_and_weight_are_separate(self):
        with self.assertRaises(ChainError) as caught:
            self.application.sampling.bind("R2", "P1", "raw", 500, "500", "kg")
        self.assertEqual(caught.exception.code, "lot_size")
        with self.assertRaises(ChainError) as caught:
            self.application.sampling.bind("R3", "P1", "raw", 100, "499", "kg")
        self.assertEqual(caught.exception.code, "weight")

    def test_first_stage_accept_releases_hold(self):
        plan = self.application.sampling.get_plan("P1")["plan"]
        revision = 1
        for index in range(plan["first_sample_size"]):
            run = self.application.sampling.inspect_unit(
                "R1", f"u{index}", "raw", self.measurement(), revision
            )
            revision = run["revision"]
        decision = self.application.sampling.decision("R1")
        self.assertEqual(decision["decision"], "accept")
        self.application.sampling.issue("R1", "qa", True, "first stage accept")
        self.assertEqual(self.application.sampling.get_run("R1")["state"], "accepted")
        self.assertEqual(self.application.graph.blocked("raw"), [])
        certificate = self.application.sampling.certificate("R1")
        self.assertEqual(certificate["plan"]["id"], "P1")
        self.assertEqual(certificate["decision_path"]["decision"], "accept")

    def test_continue_then_second_stage_reject(self):
        plan = self.application.sampling.get_plan("P1")["plan"]
        revision = 1
        for index in range(plan["first_sample_size"]):
            value = "9" if index == 0 else "15"
            run = self.application.sampling.inspect_unit(
                "R1", f"u{index}", "raw", self.measurement(value), revision
            )
            revision = run["revision"]
        self.assertEqual(
            self.application.sampling.decision("R1")["decision"], "continue"
        )
        run = self.application.sampling.advance("R1", revision)
        revision = run["revision"]
        self.assertEqual(run["state"], "stage2")
        for index in range(plan["second_sample_size"]):
            value = "9" if index < 2 else "15"
            run = self.application.sampling.inspect_unit(
                "R1", f"s{index}", "raw", self.measurement(value), revision
            )
            revision = run["revision"]
        decision = self.application.sampling.decision("R1")
        self.assertEqual(decision["decision"], "reject")
        with self.assertRaises(ChainError):
            self.application.sampling.issue("R1", "qa", True, "forced release")
        self.application.sampling.issue("R1", "qa", False, "plan rejection")
        self.assertTrue(self.application.graph.blocked("raw"))
        certificate = self.application.sampling.certificate("R1")
        self.assertEqual(certificate["decision_path"]["stage"], "stage2")

    def test_samples_cannot_repeat_or_cross_lots(self):
        self.application.sampling.inspect_unit(
            "R1", "u0", "raw", self.measurement(), 1
        )
        with self.assertRaises(ChainError) as caught:
            self.application.sampling.inspect_unit(
                "R1", "u0", "raw", self.measurement(), 2
            )
        self.assertEqual(caught.exception.code, "conflict")
        with self.assertRaises(ChainError) as caught:
            self.application.sampling.inspect_unit(
                "R1", "u9", "other-lot", self.measurement(), 2
            )
        self.assertEqual(caught.exception.code, "lot")
        exceptions = [
            item["kind"] for item in self.application.sampling.get_run("R1")["exceptions"]
        ]
        self.assertIn("duplicate_unit", exceptions)
        self.assertIn("foreign_sample", exceptions)

    def test_incomplete_sample_blocks_issuance_and_keeps_exception(self):
        plan = self.application.sampling.get_plan("P1")["plan"]
        revision = 1
        for index in range(plan["first_sample_size"] - 1):
            run = self.application.sampling.inspect_unit(
                "R1", f"u{index}", "raw", self.measurement(), revision
            )
            revision = run["revision"]
        with self.assertRaises(ChainError) as caught:
            self.application.sampling.issue("R1", "qa", True, "early")
        self.assertEqual(caught.exception.code, "quality")
        self.assertIn(
            "planned_samples_incomplete", caught.exception.details["blockers"]
        )
        self.assertTrue(
            any(
                item["kind"] == "issuance_blocked"
                for item in self.application.sampling.get_run("R1")["exceptions"]
            )
        )

    def test_plan_is_frozen(self):
        with self.assertRaises(ChainError):
            self.application.sampling.approve_plan(
                {**self.plan_request, "aql": "0.03"}
            )
        with self.assertRaises(ChainError):
            self.application.sampling.approve_plan(
                {**self.plan_request, "lot_size": 90}
            )

    def test_infeasible_plan_cannot_be_approved(self):
        with self.assertRaises(ChainError) as caught:
            self.application.sampling.approve_plan(
                {
                    **self.plan_request,
                    "id": "PBAD",
                    "lot_size": 20000,
                    "aql": "0.0005",
                    "rql": "0.002",
                }
            )
        self.assertEqual(caught.exception.code, "infeasible")

    def test_lot_change_after_binding_blocks_issuance(self):
        plan = self.application.sampling.get_plan("P1")["plan"]
        revision = 1
        for index in range(plan["first_sample_size"]):
            run = self.application.sampling.inspect_unit(
                "R1", f"u{index}", "raw", self.measurement(), revision
            )
            revision = run["revision"]
        self.application.projections.correct(
            "c1",
            "e0",
            envelope(ops=[receive(quantity="520")], at=T0),
            "quantity corrected",
        )
        with self.assertRaises(ChainError) as caught:
            self.application.sampling.issue("R1", "qa", True, "changed lot")
        self.assertIn(
            "lot_changed_since_binding", caught.exception.details["blockers"]
        )


if __name__ == "__main__":
    unittest.main()
