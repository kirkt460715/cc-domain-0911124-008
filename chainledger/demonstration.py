from .service import SupplyChain


def _sampling_demo(app):
    """Exercise first-stage accept, second-stage reject and infeasible design."""
    app.consumer.register("sampling-supplier")
    app.consumer.submit(
        {
            "id": "sampling-receipt",
            "source": "sampling-supplier",
            "offset": 0,
            "occurred_at": "2026-11-11T03:00:00Z",
            "operations": [
                {
                    "id": "sampling-receive-op",
                    "kind": "receive",
                    "lot": "incoming",
                    "quantity": "500",
                    "material": "alloy",
                    "unit": "kg",
                    "supplier": "synthetic-supplier",
                    "location": "qa-inbox",
                }
            ],
        }
    )
    app.quality.specification(
        {
            "id": "hardness-spec",
            "material": "alloy",
            "minimum_samples": 5,
            "maximum_defects": 1,
            "criteria": [
                {"name": "hardness", "unit": "HB", "lower": "10", "upper": "20"}
            ],
        }
    )
    plan_request = {
        "id": "double-plan",
        "material": "alloy",
        "lot_size": 100,
        "unit": {"kind": "piece", "label": "forged-piece"},
        "aql": "0.02",
        "rql": "0.10",
        "producer_risk": "0.05",
        "consumer_risk": "0.10",
        "kind": "double",
        "inspection_spec": "hardness-spec",
        "criteria": [{"name": "hardness"}],
    }
    app.sampling.approve_plan(plan_request)
    plan = app.sampling.get_plan("double-plan")["plan"]

    def measurement(value="15"):
        return {"hardness": {"value": value, "unit": "HB"}}

    # Path 1: a clean first sample lands on the first-stage accept region.
    app.sampling.bind("accept-run", "double-plan", "incoming", 100, "500", "kg")
    revision = 1
    for index in range(plan["first_sample_size"]):
        run = app.sampling.inspect_unit(
            "accept-run", f"a{index}", "incoming", measurement(), revision
        )
        revision = run["revision"]
    accept_decision = app.sampling.decision("accept-run")
    assert accept_decision["decision"] == "accept"
    app.sampling.issue("accept-run", "qa", True, "first stage accepted")
    accept_certificate = app.sampling.certificate("accept-run")

    # A second lot demonstrates the continue -> second-stage reject path.
    app.consumer.submit(
        {
            "id": "sampling-receipt-2",
            "source": "sampling-supplier",
            "offset": 1,
            "occurred_at": "2026-11-11T04:00:00Z",
            "operations": [
                {
                    "id": "sampling-receive-op-2",
                    "kind": "receive",
                    "lot": "incoming-2",
                    "quantity": "500",
                    "material": "alloy",
                    "unit": "kg",
                    "supplier": "synthetic-supplier",
                    "location": "qa-inbox",
                }
            ],
        }
    )
    app.sampling.bind("reject-run", "double-plan", "incoming-2", 100, "500", "kg")
    revision = 1
    for index in range(plan["first_sample_size"]):
        value = "9" if index == 0 else "15"
        run = app.sampling.inspect_unit(
            "reject-run", f"b{index}", "incoming-2", measurement(value), revision
        )
        revision = run["revision"]
    assert app.sampling.decision("reject-run")["decision"] == "continue"
    run = app.sampling.advance("reject-run", revision)
    revision = run["revision"]
    for index in range(plan["second_sample_size"]):
        value = "9" if index < 2 else "15"
        run = app.sampling.inspect_unit(
            "reject-run", f"c{index}", "incoming-2", measurement(value), revision
        )
        revision = run["revision"]
    reject_decision = app.sampling.decision("reject-run")
    assert reject_decision["decision"] == "reject"
    app.sampling.issue("reject-run", "qa", False, "second stage rejected")
    reject_certificate = app.sampling.certificate("reject-run")

    infeasible = app.sampling.design(
        {
            "lot_size": 20000,
            "aql": "0.0005",
            "rql": "0.002",
            "producer_risk": "0.05",
            "consumer_risk": "0.10",
            "kind": "any",
        }
    )
    assert infeasible["feasible"] is False
    return {
        "search_limits": app.sampling.limits(),
        "frozen_plan": plan,
        "first_stage_accept": {
            "decision": accept_certificate["decision_path"],
            "state": accept_certificate["state"],
        },
        "second_stage_reject": {
            "decision": reject_certificate["decision_path"],
            "state": reject_certificate["state"],
            "exceptions": reject_certificate["exceptions"],
        },
        "infeasible_design": infeasible,
    }


def run():
    with SupplyChain() as app:
        app.consumer.register("supplier")
        app.consumer.register("factory")
        transform = {
            "id": "split",
            "source": "factory",
            "offset": 0,
            "occurred_at": "2026-11-11T01:00:00Z",
            "operations": [
                {
                    "id": "split-op",
                    "kind": "transform",
                    "inputs": [{"lot": "raw", "quantity": "10"}],
                    "outputs": [
                        {"lot": "child", "quantity": "9", "location": "warehouse"}
                    ],
                    "waste": "1",
                    "process": "cutting",
                }
            ],
        }
        blocked = app.consumer.submit(transform)
        app.consumer.submit(
            {
                "id": "receipt",
                "source": "supplier",
                "offset": 0,
                "occurred_at": "2026-11-11T00:00:00Z",
                "operations": [
                    {
                        "id": "receive-op",
                        "kind": "receive",
                        "lot": "raw",
                        "quantity": "10",
                        "material": "alloy",
                        "unit": "kg",
                        "supplier": "synthetic-supplier",
                        "location": "inbox",
                    }
                ],
            }
        )
        app.consumer.submit(
            {
                "id": "shipment-event",
                "source": "factory",
                "offset": 1,
                "occurred_at": "2026-11-11T02:00:00Z",
                "operations": [
                    {
                        "id": "ship-op",
                        "kind": "ship",
                        "lot": "child",
                        "quantity": "4",
                        "location": "warehouse",
                        "customer": "customer-A",
                        "shipment": "shipment-A",
                    }
                ],
            }
        )
        recall = app.recalls.open("recall-A", "raw", "Synthetic trace exercise")
        conserved = app.trace.conservation_report()
        assert blocked["state"] == "blocked"
        assert app.consumer.status("split")["state"] == "applied"
        assert app.graph.stock("child") == 5
        assert recall["impact"]["shipments"][0]["path"] == ["raw", "child"]
        assert conserved["materials"][0]["received"] == "10"
        assert conserved["materials"][0]["waste"] == "1"
        sampling = _sampling_demo(app)
        return {
            "initial_state": blocked["state"],
            "resolved_state": app.consumer.status("split")["state"],
            "inventory": app.inventory.by_location(),
            "recall": recall,
            "conservation": conserved,
            "sampling": sampling,
            "check": app.check(),
        }
