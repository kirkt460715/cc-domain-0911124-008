"""Acceptance sampling plan approval and lot-bound execution.

This layer freezes a sampling design produced by
:mod:`chainledger.graph.sampling_stats` and then drives an actual incoming-lot
inspection along the frozen plan.  The governing rules are deliberately
strict:

* An approved plan freezes the lot size, the sampling unit definition, the
  inspection specification and every calculation parameter.  Plans and their
  decisions are immutable and content-addressed, so a threshold can never be
  swapped under an in-flight inspection.
* Sampling units map one-to-one to unique physical identifiers inside the lot.
  The piece count and the ledger weight are registered separately and
  reconciled; kilograms are never treated as independent sample units.
* A double plan exposes the first-stage accept / reject / continue regions and
  decides the second stage on cumulative defects.  A unit drawn in stage one is
  removed from the lot, so it cannot be drawn again in stage two.
* Issuance is blocked when planned samples are missing, the lot size has
  changed since binding, or a sample originates in another batch.  Every
  blocked attempt is retained as an exception record rather than silently
  dropped.
"""

from decimal import Decimal
from . import sampling_stats as stats
from ..types import (
    key,
    obj,
    array,
    integer,
    amount,
    text,
    canonical,
    decode,
    fingerprint,
    fail,
    ChainError,
)

RATE_BOUNDS = (Decimal("0"), Decimal("1"))


class AcceptanceSampling:
    def __init__(self, journal, graph, quality):
        self.journal = journal
        self.graph = graph
        self.quality = quality
        with journal.transaction() as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS sampling_plans(id TEXT PRIMARY KEY,
                 payload TEXT NOT NULL,digest TEXT NOT NULL)"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS sampling_runs(id TEXT PRIMARY KEY,
                 plan TEXT NOT NULL,lot TEXT NOT NULL,state TEXT NOT NULL,
                 revision INTEGER NOT NULL,baseline TEXT NOT NULL,
                 piece_count INTEGER NOT NULL,registered_weight TEXT NOT NULL,
                 weight_unit TEXT NOT NULL,result TEXT)"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS sampling_units(run TEXT NOT NULL,
                 unit_id TEXT NOT NULL,lot TEXT NOT NULL,stage INTEGER NOT NULL,
                 payload TEXT NOT NULL,PRIMARY KEY(run,unit_id))"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS sampling_exceptions(id TEXT PRIMARY KEY,
                 run TEXT NOT NULL,payload TEXT NOT NULL)"""
            )

    # ------------------------------------------------------------------ design

    def design(self, data):
        """Search without persisting, so callers can preview or reject a design."""
        self._validate_request(data)
        return stats.design_sampling_plan(
            N=integer(data["lot_size"], "lot_size", 1, 1_000_000),
            aql=float(amount(data["aql"], allow_zero=True)),
            rql=float(amount(data["rql"], allow_zero=True)),
            producer_risk=float(amount(data["producer_risk"], allow_zero=True)),
            consumer_risk=float(amount(data["consumer_risk"], allow_zero=True)),
            kind=data.get("kind", "any"),
        )

    def limits(self):
        return stats.search_limits()

    def approve_plan(self, data):
        obj(
            data,
            {
                "id",
                "material",
                "lot_size",
                "unit",
                "aql",
                "rql",
                "producer_risk",
                "consumer_risk",
                "kind",
                "inspection_spec",
                "criteria",
            },
            {
                "id",
                "material",
                "lot_size",
                "unit",
                "aql",
                "rql",
                "producer_risk",
                "consumer_risk",
                "kind",
                "inspection_spec",
                "criteria",
            },
        )
        plan_id = key(data["id"])
        material = key(data["material"])
        N = integer(data["lot_size"], "lot_size", 1, 1_000_000)
        unit = self._unit_definition(data["unit"])
        self._validate_request(data)
        spec = self.quality.spec(key(data["inspection_spec"]))
        if spec["material"] != material:
            fail("material", "plan material differs from inspection specification")
        criteria = self._freeze_criteria(data["criteria"], spec)
        design = stats.design_sampling_plan(
            N=N,
            aql=float(amount(data["aql"], allow_zero=True)),
            rql=float(amount(data["rql"], allow_zero=True)),
            producer_risk=float(amount(data["producer_risk"], allow_zero=True)),
            consumer_risk=float(amount(data["consumer_risk"], allow_zero=True)),
            kind=data["kind"],
        )
        if not design["feasible"]:
            fail("infeasible", "no sampling plan meets the stated risks", report=design)
        payload = {
            "id": plan_id,
            "kind": design["kind"],
            "material": material,
            "lot_size": N,
            "unit": unit,
            "inspection_spec": spec["id"],
            "criteria": criteria,
            "design_inputs": {
                "aql": text(amount(data["aql"], allow_zero=True)),
                "rql": text(amount(data["rql"], allow_zero=True)),
                "producer_risk": text(
                    amount(data["producer_risk"], allow_zero=True)
                ),
                "consumer_risk": text(
                    amount(data["consumer_risk"], allow_zero=True)
                ),
                "kind": data["kind"],
            },
            "plan": design["plan"],
            "risks": design["risks"],
            "oc_curve": design["oc_curve"],
            "average_sample_size": design["average_sample_size"],
            "probability_model": "hypergeometric without replacement",
            "search_limits": stats.search_limits(),
        }
        digest = fingerprint(payload)
        with self.journal.transaction() as con:
            old = self.journal.one(
                "SELECT * FROM sampling_plans WHERE id=?", (plan_id,)
            )
            if old:
                if old["digest"] != digest:
                    fail("conflict", "sampling plan is frozen and immutable")
                return decode(old["payload"])
            con.execute(
                "INSERT INTO sampling_plans VALUES(?,?,?)",
                (plan_id, canonical(payload), digest),
            )
            self.journal.record("sampling_plan_approved", plan_id, payload)
        return payload

    def get_plan(self, plan_id):
        row = self.journal.one(
            "SELECT payload,digest FROM sampling_plans WHERE id=?", (key(plan_id),)
        )
        if row is None:
            fail("missing", "sampling plan not found")
        payload = decode(row["payload"])
        if fingerprint(payload) != row["digest"]:
            fail("integrity", "sampling plan digest mismatch")
        return payload

    # --------------------------------------------------------------- run setup

    def bind(self, run_id, plan_id, lot_id, piece_count, weight, weight_unit):
        key(run_id)
        with self.journal.transaction() as con:
            existing = self.journal.one(
                "SELECT * FROM sampling_runs WHERE id=?", (run_id,)
            )
            if existing:
                if (
                    existing["plan"] != plan_id
                    or existing["lot"] != lot_id
                    or existing["piece_count"] != piece_count
                    or existing["registered_weight"]
                    != text(amount(weight, allow_zero=True))
                    or existing["weight_unit"] != key(weight_unit)
                ):
                    fail("conflict", "sampling run identity differs")
                return self.get_run(run_id)
            plan = self.get_plan(plan_id)
            lot = self.graph.lot(lot_id)
            if lot["material"] != plan["material"]:
                fail("material", "lot material differs from approved plan")
            pieces = integer(piece_count, "piece_count", 1, 1_000_000)
            if pieces != plan["lot_size"]:
                fail(
                    "lot_size",
                    "registered piece count does not match frozen lot size",
                    registered=pieces,
                    frozen=plan["lot_size"],
                )
            ledger_weight = amount(weight, allow_zero=True)
            wunit = key(weight_unit)
            if wunit != lot["unit"]:
                fail("unit", "weight unit differs from lot ledger unit")
            if ledger_weight != amount(lot["quantity"]):
                fail(
                    "weight",
                    "registered weight does not reconcile with the ledger quantity",
                    registered=text(ledger_weight),
                    ledger=lot["quantity"],
                )
            baseline = fingerprint(lot)
            con.execute(
                """INSERT INTO sampling_runs(id,plan,lot,state,revision,baseline,
                 piece_count,registered_weight,weight_unit,result)
                 VALUES(?,?,?, 'stage1', 1, ?, ?, ?, ?, NULL)""",
                (
                    run_id,
                    plan_id,
                    lot_id,
                    baseline,
                    pieces,
                    text(ledger_weight),
                    wunit,
                ),
            )
            hold_id = "sampling:" + run_id
            if self.journal.one("SELECT id FROM holds WHERE id=?", (hold_id,)):
                fail("conflict", "sampling hold identity collides")
            con.execute(
                "INSERT INTO holds VALUES(?,?,?,1)",
                (hold_id, lot_id, "Acceptance sampling awaiting disposition"),
            )
            self.journal.record(
                "sampling_run_bound",
                run_id,
                {
                    "plan": plan_id,
                    "lot": lot_id,
                    "piece_count": pieces,
                    "weight": text(ledger_weight),
                    "weight_unit": wunit,
                    "baseline": baseline,
                },
            )
        return self.get_run(run_id)

    def get_run(self, run_id):
        row = self.journal.one(
            "SELECT * FROM sampling_runs WHERE id=?", (key(run_id),)
        )
        if row is None:
            fail("missing", "sampling run not found")
        units = [
            decode(item["payload"])
            for item in self.journal.rows(
                "SELECT payload FROM sampling_units WHERE run=? ORDER BY stage,unit_id",
                (run_id,),
            )
        ]
        exceptions = [
            decode(item["payload"])
            for item in self.journal.rows(
                "SELECT payload FROM sampling_exceptions WHERE run=? ORDER BY id",
                (run_id,),
            )
        ]
        return {
            **row,
            "result": decode(row["result"]) if row["result"] else None,
            "units": units,
            "exceptions": exceptions,
        }

    # ------------------------------------------------------------- inspection

    def _guarded(self, run_id, work):
        """Run a mutating workflow, retaining any exception after rollback.

        ``work`` receives a list to append exception payloads to.  When it
        raises :class:`ChainError`, the business transaction is rolled back and
        the collected exceptions are persisted in a separate transaction so a
        blocked attempt leaves a durable audit trail without partially
        applying the operation.
        """
        exceptions = []
        try:
            with self.journal.transaction():
                return work(exceptions)
        except ChainError as exc:
            if exceptions:
                with self.journal.transaction():
                    for payload in exceptions:
                        self._store_exception(run_id, payload)
            raise

    def inspect_unit(self, run_id, unit_id, source_lot, measurements, expected_revision):
        key(unit_id)

        def work(exceptions):
            run = self.get_run(run_id)
            if run["revision"] != expected_revision:
                fail("conflict", "sampling run revision changed")
            if run["state"] in {"accepted", "rejected"}:
                fail("state", "sampling run is already decided")
            plan = self.get_plan(run["plan"])
            self._check_lot_intact(run, plan, exceptions)
            if key(source_lot) != run["lot"]:
                exceptions.append(
                    {
                        "kind": "foreign_sample",
                        "unit": unit_id,
                        "source_lot": source_lot,
                        "expected_lot": run["lot"],
                    }
                )
                fail("lot", "sample unit does not belong to the bound lot")
            duplicate = self.journal.one(
                "SELECT 1 FROM sampling_units WHERE run=? AND unit_id=?",
                (run_id, unit_id),
            )
            if duplicate:
                exceptions.append(
                    {"kind": "duplicate_unit", "unit": unit_id}
                )
                fail("conflict", "sampling unit already inspected in this run")
            stage = 1 if run["state"] == "stage1" else 2
            self._assert_stage_capacity(run, plan, stage, unit_id, exceptions)
            verdict = self._measure(plan, measurements)
            payload = {
                "unit_id": unit_id,
                "lot": run["lot"],
                "stage": stage,
                "measurements": verdict["measurements"],
                "failed_criteria": verdict["failures"],
                "defective": bool(verdict["failures"]),
            }
            self.journal.connection.execute(
                "INSERT INTO sampling_units VALUES(?,?,?,?,?)",
                (run_id, unit_id, run["lot"], stage, canonical(payload)),
            )
            self.journal.connection.execute(
                "UPDATE sampling_runs SET revision=revision+1 WHERE id=?",
                (run_id,),
            )
            self.journal.record("sampling_unit_inspected", run_id, payload)
            return self.get_run(run_id)

        return self._guarded(run_id, work)

    def advance(self, run_id, expected_revision):
        """Move a double plan from stage one into stage two.

        The transition is allowed only when the first sample is complete and
        its defect count lands in the continue region.  An accepted or
        rejected first stage is final, and a single plan has no second stage.
        """

        def work(exceptions):
            run = self.get_run(run_id)
            if run["revision"] != expected_revision:
                fail("conflict", "sampling run revision changed")
            if run["state"] in {"accepted", "rejected"}:
                fail("state", "sampling run is already decided")
            plan = self.get_plan(run["plan"])
            if plan["kind"] != "double":
                exceptions.append({"kind": "advance_single_plan"})
                fail("state", "single sampling plans have no second stage")
            if run["state"] != "stage1":
                exceptions.append(
                    {"kind": "advance_wrong_stage", "state": run["state"]}
                )
                fail("state", "run is not in the first stage")
            decision = self.decision(run_id)
            first = [u for u in run["units"] if u["stage"] == 1]
            if len(first) != plan["plan"]["first_sample_size"]:
                exceptions.append(
                    {
                        "kind": "advance_incomplete",
                        "observed": len(first),
                        "required": plan["plan"]["first_sample_size"],
                    }
                )
                fail("state", "first-stage sample is incomplete")
            if decision["decision"] != "continue":
                exceptions.append(
                    {
                        "kind": "advance_final_first_stage",
                        "first_stage_decision": decision["decision"],
                    }
                )
                fail(
                    "state",
                    "first stage is already decisive; no second stage is opened",
                )
            self.journal.connection.execute(
                "UPDATE sampling_runs SET state='stage2',revision=revision+1 WHERE id=?",
                (run_id,),
            )
            self.journal.record(
                "sampling_stage_advanced",
                run_id,
                {"first_stage": decision},
            )
            return self.get_run(run_id)

        return self._guarded(run_id, work)

    def decision(self, run_id):
        """Current deterministic decision path without mutating the run."""
        run = self.get_run(run_id)
        plan = self.get_plan(run["plan"])
        units = run["units"]
        first = [u for u in units if u["stage"] == 1]
        second = [u for u in units if u["stage"] == 2]
        d1 = sum(1 for u in first if u["defective"])
        d2 = sum(1 for u in second if u["defective"])
        lot_intact = self._lot_intact(run, plan)
        base = {
            "run": run_id,
            "plan": plan["id"],
            "kind": plan["kind"],
            "lot_intact": lot_intact,
            "first_sample_observed": len(first),
            "second_sample_observed": len(second),
            "first_defects": d1,
            "second_defects": d2,
            "cumulative_defects": d1 + d2,
        }
        if plan["kind"] == "single":
            n = plan["plan"]["sample_size"]
            c = plan["plan"]["acceptance_number"]
            return {
                **base,
                "required_sample_size": n,
                "acceptance_number": c,
                "stage": "stage1",
                "complete": len(first) == n,
                "decision": self._single_verdict(len(first), d1, n, c)
                if len(first) == n
                else "continue",
            }
        p = plan["plan"]
        return self._double_decision(base, p, len(first), d1, len(second), d2)

    def issue(self, run_id, reviewer, release, reason):
        key(reviewer)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            fail("input", "issuance needs a bounded reason")
        if not isinstance(release, bool):
            fail("input", "release must be explicit boolean")

        def work(exceptions):
            run = self.get_run(run_id)
            if run["state"] in {"accepted", "rejected"}:
                fail("state", "sampling run is already decided")
            plan = self.get_plan(run["plan"])
            decision = self.decision(run_id)
            blockers = self._blockers(run, plan, decision)
            if blockers:
                exceptions.append(
                    {"kind": "issuance_blocked", "blockers": blockers}
                )
                fail("quality", "issuance blocked", blockers=blockers)
            if release and decision["decision"] != "accept":
                exceptions.append(
                    {
                        "kind": "release_against_plan",
                        "plan_decision": decision["decision"],
                    }
                )
                fail(
                    "quality",
                    "plan does not accept this lot; release is forbidden",
                    decision=decision,
                )
            final_state = "accepted" if release else "rejected"
            payload = {
                "run": run_id,
                "plan": plan["id"],
                "reviewer": reviewer,
                "release": release,
                "reason": reason,
                "decision_path": decision,
                "plan_digest": fingerprint(plan),
            }
            self.journal.connection.execute(
                "UPDATE sampling_runs SET state=?,revision=revision+1,result=? WHERE id=?",
                (final_state, canonical(payload), run_id),
            )
            if release:
                self.journal.connection.execute(
                    "UPDATE holds SET active=0 WHERE id=?",
                    ("sampling:" + run_id,),
                )
            self.journal.record("sampling_disposition", run_id, payload)
            return self.get_run(run_id)

        return self._guarded(run_id, work)

    def certificate(self, run_id):
        """Release evidence preserving the frozen plan and actual decision path."""
        run = self.get_run(run_id)
        if run["state"] not in {"accepted", "rejected"}:
            fail("state", "run has no final disposition yet")
        plan = self.get_plan(run["plan"])
        lot = self.graph.lot(run["lot"])
        disposition = run["result"]
        return {
            "run": run_id,
            "state": run["state"],
            "lot": {
                "id": lot["id"],
                "material": lot["material"],
                "piece_count": run["piece_count"],
                "registered_weight": run["registered_weight"],
                "weight_unit": run["weight_unit"],
                "ledger_quantity": lot["quantity"],
                "ledger_unit": lot["unit"],
            },
            "plan": plan,
            "decision_path": disposition["decision_path"] if disposition else None,
            "disposition": disposition,
            "sample_digest": fingerprint(run["units"]),
            "exceptions": run["exceptions"],
        }

    # ------------------------------------------------------------- internals

    def _validate_request(self, data):
        # Only the risk fields are required; plan approval carries additional
        # identity fields that must not be rejected here.
        obj(data, required={"lot_size", "aql", "rql", "producer_risk", "consumer_risk"})
        kind = data.get("kind", "any")
        if kind not in {"any", "single", "double"}:
            fail("input", "kind must be any, single or double")
        aql = amount(data["aql"], allow_zero=True)
        rql = amount(data["rql"], allow_zero=True)
        alpha = amount(data["producer_risk"], allow_zero=True)
        beta = amount(data["consumer_risk"], allow_zero=True)
        for name, value in (
            ("aql", aql),
            ("rql", rql),
            ("producer_risk", alpha),
            ("consumer_risk", beta),
        ):
            if not RATE_BOUNDS[0] <= value <= RATE_BOUNDS[1]:
                fail("input", "rate must be within [0,1]", field=name)
        if aql >= rql:
            fail("input", "AQL must be strictly below RQL")

    def _unit_definition(self, data):
        obj(data, {"kind", "label"}, {"kind", "label"})
        kind = key(data["kind"])
        if kind not in {"piece", "package", "container"}:
            fail("input", "sampling unit kind must be piece, package or container")
        return {"kind": kind, "label": key(data["label"])}

    def _freeze_criteria(self, criteria, spec):
        names = []
        for item in array(criteria, 1, 100):
            obj(item, {"name"}, {"name"})
            names.append(key(item["name"]))
        spec_names = {c["name"] for c in spec["criteria"]}
        if set(names) - spec_names or len(names) != len(set(names)):
            fail("input", "plan criteria must be unique subset of the inspection spec")
        return sorted(names)

    def _measure(self, plan, measurements):
        spec = self.quality.spec(plan["inspection_spec"])
        wanted = set(plan["criteria"])
        obj(measurements)
        if set(measurements) != wanted:
            fail("input", "measurements must cover exactly the frozen criteria")
        normalized = {}
        failures = []
        criteria = {c["name"]: c for c in spec["criteria"]}
        for name in plan["criteria"]:
            criterion = criteria[name]
            item = measurements[name]
            obj(item, {"value", "unit"}, {"value", "unit"})
            if item["unit"] != criterion["unit"]:
                fail("unit", "measurement unit differs from frozen specification")
            value = amount(item["value"], allow_zero=True)
            normalized[name] = {"value": text(value), "unit": item["unit"]}
            if not Decimal(criterion["lower"]) <= value <= Decimal(criterion["upper"]):
                failures.append(name)
        return {"measurements": normalized, "failures": sorted(failures)}

    def _assert_stage_capacity(self, run, plan, stage, unit_id, exceptions):
        units = run["units"]
        if plan["kind"] == "single":
            cap = plan["plan"]["sample_size"]
            if len(units) >= cap:
                exceptions.append(
                    {"kind": "sample_over_plan", "unit": unit_id, "cap": cap}
                )
                fail("limit", "all planned samples already recorded")
            return
        p = plan["plan"]
        if stage == 1:
            if len(units) >= p["first_sample_size"]:
                exceptions.append(
                    {"kind": "stage_full", "unit": unit_id, "stage": 1}
                )
                fail("state", "first stage already has its full sample")
        else:
            second = [u for u in units if u["stage"] == 2]
            if len(second) >= p["second_sample_size"]:
                exceptions.append(
                    {"kind": "stage_full", "unit": unit_id, "stage": 2}
                )
                fail("state", "second stage already has its full sample")

    def _single_verdict(self, observed, defects, n, c):
        if observed < n:
            return "continue"
        return "accept" if defects <= c else "reject"

    def _double_decision(self, base, p, n1_seen, d1, n2_seen, d2):
        a1 = p["first_accept"]
        r1 = p["first_reject"]
        a2 = p["cumulative_accept"]
        stage1_full = n1_seen == p["first_sample_size"]
        if not stage1_full:
            return {
                **base,
                "stage": "stage1",
                "regions": {
                    "accept_when_first_defects_lte": a1,
                    "reject_when_first_defects_gte": r1,
                    "continue_when_between": [a1 + 1, r1 - 1],
                },
                "complete": False,
                "decision": "continue",
            }
        if d1 <= a1:
            verdict = "accept"
        elif d1 >= r1:
            verdict = "reject"
        else:
            verdict = "continue"
        result = {
            **base,
            "stage": "stage1",
            "regions": {
                "accept_when_first_defects_lte": a1,
                "reject_when_first_defects_gte": r1,
                "continue_when_between": [a1 + 1, r1 - 1],
            },
            "first_stage_decision": verdict,
            "complete": False,
            "decision": verdict,
        }
        if verdict != "continue":
            result["complete"] = True
            return result
        # Continue region: second stage decides on cumulative defects.
        stage2_full = n2_seen == p["second_sample_size"]
        result.update(
            {
                "stage": "stage2",
                "cumulative_acceptance_number": a2,
                "second_stage_required": p["second_sample_size"],
                "complete": stage2_full,
                "decision": (
                    "accept"
                    if stage2_full and d1 + d2 <= a2
                    else "reject"
                    if stage2_full
                    else "continue"
                ),
            }
        )
        return result

    def _lot_intact(self, run, plan):
        if run["piece_count"] != plan["lot_size"]:
            return False
        current = self.graph.lot(run["lot"])
        return fingerprint(current) == run["baseline"]

    def _check_lot_intact(self, run, plan, exceptions):
        if not self._lot_intact(run, plan):
            exceptions.append(
                {"kind": "lot_changed", "baseline": run["baseline"]}
            )
            fail("lot_size", "lot changed since the run was bound")

    def _blockers(self, run, plan, decision):
        blockers = []
        if not decision["lot_intact"]:
            blockers.append("lot_changed_since_binding")
        if not decision["complete"]:
            blockers.append("planned_samples_incomplete")
        if run["piece_count"] != plan["lot_size"]:
            blockers.append("piece_count_mismatch")
        if Decimal(run["registered_weight"]) != Decimal(
            self.graph.lot(run["lot"])["quantity"]
        ):
            blockers.append("ledger_weight_mismatch")
        return blockers

    def _store_exception(self, run_id, payload):
        count = self.journal.one(
            "SELECT COUNT(*) AS n FROM sampling_exceptions WHERE run=?",
            (run_id,),
        )["n"]
        exc_id = run_id + ":exc:" + str(count + 1)
        record = {"id": exc_id, "run": run_id, **payload}
        self.journal.connection.execute(
            "INSERT INTO sampling_exceptions VALUES(?,?,?)",
            (exc_id, run_id, canonical(record)),
        )
        self.journal.record("sampling_exception", exc_id, record)

    def _exception(self, run_id, payload):
        with self.journal.transaction():
            self._store_exception(run_id, payload)
