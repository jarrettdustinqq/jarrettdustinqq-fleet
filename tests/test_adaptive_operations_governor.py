from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "ops" / "adaptive_operations_governor.py"
SPEC = importlib.util.spec_from_file_location("adaptive_operations_governor", MODULE_PATH)
assert SPEC and SPEC.loader
AOG = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AOG
SPEC.loader.exec_module(AOG)


def make_loop(
    loop_id: str,
    *,
    priority: str = "P3",
    hard: bool = False,
    weight: str = "LIGHT",
    next_due_at: str = "2026-09-02T04:00:00-05:00",
) -> dict:
    return {
        "loop_id": loop_id,
        "objective_id": f"OBJ.{loop_id}",
        "layer": "LOGICAL",
        "scheduler_id": "scheduler-1",
        "parent_loop_id": "LOOP.MUXA",
        "trigger": "fixed phase or source event",
        "enabled": True,
        "triggered": False,
        "hard_obligation": hard,
        "lifecycle_state": "ACTIVE",
        "priority_class": priority,
        "weight_class": weight,
        "required_authority": "L0",
        "current_authority": "L0",
        "authority_valid": True,
        "dependencies": [
            {
                "dependency_id": "DEP.SOURCE",
                "status": "READY",
                "critical": True,
                "fresh_until": "2026-09-02T06:00:00-05:00",
            }
        ],
        "cadence": {
            "min_seconds": 3600,
            "max_seconds": 259200,
            "current_seconds": 86400,
            "phase": "04:00",
        },
        "resource_budget": {
            "max_calls_per_run": 10,
            "estimated_calls": 2,
            "max_runtime_seconds": 600,
            "estimated_runtime_seconds": 60,
            "max_cost_usd": 1.0,
            "estimated_cost_usd": 0.01,
        },
        "success_evidence": ["first-party exact readback"],
        "verified_abstention_criteria": ["unchanged source with current evidence"],
        "idempotency_rule": "source stable ID plus due phase",
        "adaptation_bounds": {
            "allowed_actions": ["SKIP_ONCE", "PAUSE", "RESUME", "SET_CADENCE"],
            "no_change_threshold": 3,
            "failure_pause_threshold": 2,
            "starvation_threshold": 3,
        },
        "pause_rule": "critical dependency unavailable after two failures",
        "retirement_rule": "explicit objective closure plus evidence",
        "expected_benefit": 0.5,
        "probability_success": 0.7,
        "information_gain": 0.4,
        "external_branching_value": 0.3,
        "external_progress_probability": 0.2,
        "employment_compatibility": 1.0,
        "cost": 0.1,
        "attention_burden": 0.1,
        "risk": 0.1,
        "no_material_change_streak": 0,
        "consecutive_failures": 0,
        "deferred_cycles": 0,
        "resume_triggered": False,
        "last_run_at": "2026-09-01T04:00:00-05:00",
        "next_due_at": next_due_at,
        "deadline_at": None,
        "source_fresh_until": "2026-09-02T06:00:00-05:00",
        "last_material_outcome_at": "2026-09-01T04:00:00-05:00",
        "event_key": "",
        "last_event_key": "",
        "contradictions": [],
        "evidence_refs": [f"EV.{loop_id}"],
    }


def make_snapshot(*, requested_mode: str = "SHADOW") -> dict:
    return {
        "schema_version": AOG.SNAPSHOT_SCHEMA_VERSION,
        "registry_version": AOG.LOOP_REGISTRY_VERSION,
        "snapshot_at": "2026-09-02T04:10:00-05:00",
        "principal_id": "jarrett",
        "requested_mode": requested_mode,
        "policy_hash": AOG.POLICY_HASH,
        "journal_gate": {
            "degradation_state": "DEGRADED",
            "proof_streak": 1,
            "required_streak": 5,
            "provider_atomic_audit_cohort": True,
            "exact_cardinality_verified": True,
            "exact_idempotency_verified": True,
            "neighbor_integrity_verified": True,
            "state_after_audit_verified": True,
            "final_audit_state_readback_verified": True,
            "historical_t05_loss_preserved": True,
            "last_qualified_run": "MUXA.RUN.20260902T03",
        },
        "heartbeat": {
            "fixed_primitive": True,
            "replacement_requested": False,
            "reported_healthy": True,
            "last_seen_at": "2026-09-02T04:00:00-05:00",
            "max_age_seconds": 7200,
        },
        "registry_health": {
            "snapshot_readback_verified": True,
            "critical_state_fresh": True,
            "critical_contradictions": [],
        },
        "capacity": {"max_dispatches": 4, "max_heavy": 2, "max_calls": 20},
        "loops": [
            make_loop("LOOP.P1", priority="P1", hard=True),
            make_loop("LOOP.P3", priority="P3", hard=False, weight="HEAVY"),
        ],
        "advisor_proposals": [],
        "recent_outcomes": [],
    }


def clear_journal(snapshot: dict) -> None:
    snapshot["journal_gate"]["degradation_state"] = "CLEARED"
    snapshot["journal_gate"]["proof_streak"] = 5
    snapshot["journal_gate"]["last_qualified_run"] = "MUXA.RUN.20260902T07"


def add_canary_authority(snapshot: dict, loop_id: str = "LOOP.P3") -> None:
    snapshot["canary_authority"] = {
        "grant_id": "AUTH.AOG.CANARY.001",
        "principal_id": "jarrett",
        "loop_id": loop_id,
        "allowed_actions": ["SET_CADENCE", "SKIP_ONCE", "PAUSE", "RESUME"],
        "issued_at": "2026-09-02T04:00:00-05:00",
        "expires_at": "2026-09-03T04:00:00-05:00",
        "max_mutations": 1,
        "rollback_required": True,
        "verification_method": "scheduler plus JACS exact readback",
    }


class ContractTests(unittest.TestCase):
    def test_duplicate_loop_ids_are_rejected(self) -> None:
        snapshot = make_snapshot()
        snapshot["loops"].append(copy.deepcopy(snapshot["loops"][0]))
        with self.assertRaisesRegex(AOG.ContractError, "duplicate loop_id"):
            AOG.evaluate(snapshot)

    def test_naive_timestamp_is_rejected(self) -> None:
        snapshot = make_snapshot()
        snapshot["snapshot_at"] = "2026-09-02T04:10:00"
        with self.assertRaisesRegex(AOG.ContractError, "timezone"):
            AOG.evaluate(snapshot)

    def test_advisor_cannot_smuggle_objective_or_authority_change(self) -> None:
        snapshot = make_snapshot()
        snapshot["advisor_proposals"] = [
            {
                "proposal_id": "P.BAD",
                "loop_id": "LOOP.P3",
                "action": "SET_CADENCE",
                "target_cadence_seconds": 172800,
                "reason": "try to broaden scope",
                "confidence": 0.8,
                "evidence_refs": ["EV.1"],
                "objective_id": "OBJ.REPLACEMENT",
            }
        ]
        with self.assertRaisesRegex(AOG.ContractError, "forbidden keys"):
            AOG.evaluate(snapshot)

    def test_verified_outcome_requires_evidence(self) -> None:
        snapshot = make_snapshot()
        snapshot["recent_outcomes"] = [
            {
                "outcome_id": "OUT.1",
                "loop_id": "LOOP.P3",
                "outcome_class": "VERIFIED_INFORMATION_GAIN",
                "observed_at": snapshot["snapshot_at"],
                "evidence_refs": [],
            }
        ]
        with self.assertRaisesRegex(AOG.ContractError, "requires evidence_refs"):
            AOG.evaluate(snapshot)


class GateTests(unittest.TestCase):
    def test_reopened_muxa_degradation_forces_shadow(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        add_canary_authority(snapshot)
        result = AOG.evaluate(snapshot)
        self.assertEqual("SHADOW", result["effective_mode"])
        self.assertFalse(result["production_mutation_allowed"])
        self.assertIn(
            "INSUFFICIENT_CONSECUTIVE_CLEAN_RUNS",
            result["gates"]["fail_closed_reasons"],
        )

    def test_partial_audit_cardinality_blocks_canary(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot)
        snapshot["journal_gate"]["exact_cardinality_verified"] = False
        result = AOG.evaluate(snapshot)
        self.assertEqual("SHADOW", result["effective_mode"])
        self.assertIn("AUDIT_CARDINALITY_UNVERIFIED", result["gates"]["fail_closed_reasons"])

    def test_state_after_audit_readback_blocks_canary(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot)
        snapshot["journal_gate"]["state_after_audit_verified"] = False
        result = AOG.evaluate(snapshot)
        self.assertIn("STATE_AFTER_AUDIT_UNVERIFIED", result["gates"]["fail_closed_reasons"])

    def test_historical_t05_loss_must_remain_preserved(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot)
        snapshot["journal_gate"]["historical_t05_loss_preserved"] = False
        result = AOG.evaluate(snapshot)
        self.assertIn("HISTORICAL_T05_LOSS_NOT_PRESERVED", result["gates"]["fail_closed_reasons"])

    def test_missed_heartbeat_blocks_canary(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot)
        snapshot["heartbeat"]["last_seen_at"] = "2026-09-01T20:00:00-05:00"
        result = AOG.evaluate(snapshot)
        self.assertIn("HEARTBEAT_STALE", result["gates"]["fail_closed_reasons"])

    def test_governor_cannot_replace_primitive_heartbeat(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot)
        snapshot["heartbeat"]["replacement_requested"] = True
        result = AOG.evaluate(snapshot)
        self.assertIn("HEARTBEAT_REPLACEMENT_REQUESTED", result["gates"]["fail_closed_reasons"])

    def test_policy_drift_blocks_canary(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot)
        snapshot["policy_hash"] = "0" * 64
        result = AOG.evaluate(snapshot)
        self.assertIn("POLICY_HASH_DRIFT", result["gates"]["fail_closed_reasons"])

    def test_open_registry_contradiction_blocks_canary(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot)
        snapshot["registry_health"]["critical_contradictions"] = ["State conflicts with Audit"]
        result = AOG.evaluate(snapshot)
        self.assertIn("CRITICAL_REGISTRY_CONTRADICTIONS_OPEN", result["gates"]["fail_closed_reasons"])

    def test_canary_authority_must_be_exactly_one_mutation(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot)
        snapshot["canary_authority"]["max_mutations"] = 2
        result = AOG.evaluate(snapshot)
        self.assertIn(
            "CANARY_AUTHORITY_MUST_ALLOW_EXACTLY_ONE_MUTATION",
            result["gates"]["fail_closed_reasons"],
        )


class DispatchTests(unittest.TestCase):
    def test_duplicate_source_event_is_rejected(self) -> None:
        snapshot = make_snapshot()
        loop = snapshot["loops"][1]
        loop["event_key"] = "provider:event:123"
        loop["last_event_key"] = "provider:event:123"
        result = AOG.evaluate(snapshot)
        rejected = {item["loop_id"]: item for item in result["dispatch_plan"]["rejected"]}
        self.assertIn("DUPLICATE_SOURCE_EVENT", rejected["LOOP.P3"]["reasons"])

    def test_stale_source_is_rejected(self) -> None:
        snapshot = make_snapshot()
        snapshot["loops"][1]["source_fresh_until"] = "2026-09-02T03:00:00-05:00"
        result = AOG.evaluate(snapshot)
        rejected = {item["loop_id"]: item for item in result["dispatch_plan"]["rejected"]}
        self.assertIn("SOURCE_STALE", rejected["LOOP.P3"]["reasons"])

    def test_unavailable_dependency_is_rejected(self) -> None:
        snapshot = make_snapshot()
        snapshot["loops"][1]["dependencies"][0]["status"] = "UNAVAILABLE"
        result = AOG.evaluate(snapshot)
        rejected = {item["loop_id"]: item for item in result["dispatch_plan"]["rejected"]}
        self.assertTrue(any(reason.startswith("DEPENDENCY_UNAVAILABLE") for reason in rejected["LOOP.P3"]["reasons"]))

    def test_loop_contradiction_is_rejected(self) -> None:
        snapshot = make_snapshot()
        snapshot["loops"][1]["contradictions"] = ["provider says open; registry says closed"]
        result = AOG.evaluate(snapshot)
        rejected = {item["loop_id"]: item for item in result["dispatch_plan"]["rejected"]}
        self.assertIn("LOOP_CONTRADICTIONS_OPEN", rejected["LOOP.P3"]["reasons"])

    def test_hard_obligation_prevents_priority_inversion(self) -> None:
        snapshot = make_snapshot()
        p1 = snapshot["loops"][0]
        p1.update(
            {
                "expected_benefit": 0.0,
                "probability_success": 0.0,
                "information_gain": 0.0,
                "external_branching_value": 0.0,
                "external_progress_probability": 0.0,
                "cost": 1.0,
                "risk": 1.0,
            }
        )
        p3 = snapshot["loops"][1]
        p3.update(
            {
                "expected_benefit": 1.0,
                "probability_success": 1.0,
                "information_gain": 1.0,
                "external_branching_value": 1.0,
                "external_progress_probability": 1.0,
                "cost": 0.0,
                "risk": 0.0,
            }
        )
        result = AOG.evaluate(snapshot)
        self.assertEqual("LOOP.P1", result["dispatch_plan"]["selected"][0]["loop_id"])

    def test_deadline_pressure_increases_score(self) -> None:
        snapshot = make_snapshot()
        loop = snapshot["loops"][1]
        without, _ = AOG.score_loop(loop, AOG.loop_eligibility(loop, AOG._parse_time(snapshot["snapshot_at"], "snapshot_at")))
        loop["deadline_at"] = "2026-09-02T04:20:00-05:00"
        with_deadline, _ = AOG.score_loop(loop, AOG.loop_eligibility(loop, AOG._parse_time(snapshot["snapshot_at"], "snapshot_at")))
        self.assertGreater(with_deadline, without)

    def test_starvation_boost_is_bounded_below_hard_obligation(self) -> None:
        snapshot = make_snapshot()
        snapshot["loops"][1]["deferred_cycles"] = 100
        result = AOG.evaluate(snapshot)
        self.assertEqual("LOOP.P1", result["dispatch_plan"]["selected"][0]["loop_id"])

    def test_heavy_capacity_is_enforced(self) -> None:
        snapshot = make_snapshot()
        snapshot["capacity"]["max_heavy"] = 1
        another = make_loop("LOOP.HEAVY2", priority="P2", weight="HEAVY")
        snapshot["loops"].append(another)
        result = AOG.evaluate(snapshot)
        selected_heavy = [item for item in result["dispatch_plan"]["selected"] if item["weight_class"] == "HEAVY"]
        self.assertEqual(1, len(selected_heavy))

    def test_hard_obligation_starvation_is_diagnosed(self) -> None:
        snapshot = make_snapshot()
        snapshot["capacity"]["max_dispatches"] = 1
        snapshot["loops"].insert(0, make_loop("LOOP.P0", priority="P0", hard=True))
        result = AOG.evaluate(snapshot)
        diagnostics = result["dispatch_plan"]["diagnostics"]
        self.assertTrue(any(item.startswith("HARD_OBLIGATION_STARVED") for item in diagnostics))


class AdaptationTests(unittest.TestCase):
    def test_repeated_no_change_proposes_slower_cadence_in_shadow(self) -> None:
        snapshot = make_snapshot()
        loop = snapshot["loops"][1]
        loop["no_material_change_streak"] = 3
        result = AOG.evaluate(snapshot)
        proposal = next(item for item in result["adaptation_proposals"] if item["loop_id"] == "LOOP.P3")
        self.assertEqual("SET_CADENCE", proposal["action"])
        self.assertEqual("SHADOW_ACCEPTED", proposal["status"])
        self.assertFalse(proposal["mutation_allowed"])

    def test_executor_failure_and_dependency_loss_propose_pause(self) -> None:
        snapshot = make_snapshot()
        loop = snapshot["loops"][1]
        loop["consecutive_failures"] = 2
        loop["dependencies"][0]["status"] = "UNAVAILABLE"
        result = AOG.evaluate(snapshot)
        proposal = next(item for item in result["adaptation_proposals"] if item["loop_id"] == "LOOP.P3")
        self.assertEqual("PAUSE", proposal["action"])

    def test_unauthorized_advisor_loop_is_rejected(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot, "LOOP.P3")
        snapshot["advisor_proposals"] = [
            {
                "proposal_id": "P.OUTSIDE",
                "loop_id": "LOOP.P1",
                "action": "SET_CADENCE",
                "target_cadence_seconds": 172800,
                "reason": "outside grant",
                "confidence": 1.0,
                "evidence_refs": ["EV.P1"],
            }
        ]
        result = AOG.evaluate(snapshot)
        proposal = next(item for item in result["adaptation_proposals"] if item["proposal_id"] == "P.OUTSIDE")
        self.assertIn("PROPOSAL_OUTSIDE_CANARY_LOOP", proposal["reasons"])
        self.assertFalse(proposal["mutation_allowed"])

    def test_cadence_outside_bounds_is_rejected(self) -> None:
        snapshot = make_snapshot()
        snapshot["advisor_proposals"] = [
            {
                "proposal_id": "P.TOO_SLOW",
                "loop_id": "LOOP.P3",
                "action": "SET_CADENCE",
                "target_cadence_seconds": 999999,
                "reason": "too slow",
                "confidence": 1.0,
                "evidence_refs": ["EV.P3"],
            }
        ]
        result = AOG.evaluate(snapshot)
        proposal = next(item for item in result["adaptation_proposals"] if item["proposal_id"] == "P.TOO_SLOW")
        self.assertIn("TARGET_CADENCE_OUTSIDE_BOUNDS", proposal["reasons"])

    def test_hard_deadline_loop_cannot_be_skipped(self) -> None:
        snapshot = make_snapshot()
        loop = snapshot["loops"][0]
        loop["deadline_at"] = "2026-09-02T04:20:00-05:00"
        snapshot["advisor_proposals"] = [
            {
                "proposal_id": "P.SKIP_HARD",
                "loop_id": "LOOP.P1",
                "action": "SKIP_ONCE",
                "reason": "unsafe skip",
                "confidence": 1.0,
                "evidence_refs": ["EV.P1"],
            }
        ]
        result = AOG.evaluate(snapshot)
        proposal = next(item for item in result["adaptation_proposals"] if item["proposal_id"] == "P.SKIP_HARD")
        self.assertIn("HARD_OBLIGATION_CANNOT_BE_SKIPPED", proposal["reasons"])
        self.assertIn("DEADLINE_RELEVANT_LOOP_CANNOT_BE_SKIPPED", proposal["reasons"])

    def test_clean_canary_emits_one_reversible_intent(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot, "LOOP.P3")
        snapshot["loops"][1]["no_material_change_streak"] = 3
        result = AOG.evaluate(snapshot)
        self.assertEqual("CANARY", result["effective_mode"])
        self.assertTrue(result["production_mutation_allowed"])
        self.assertEqual(1, len(result["mutation_intents"]))
        intent = result["mutation_intents"][0]
        self.assertEqual(intent["before"], intent["rollback"]["restore"])
        self.assertEqual(86400, intent["before"]["cadence_seconds"])
        self.assertEqual(172800, intent["after"]["cadence_seconds"])
        self.assertEqual(intent["before"]["phase"], intent["after"]["phase"])

    def test_canary_never_emits_more_than_one_mutation(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        clear_journal(snapshot)
        add_canary_authority(snapshot, "LOOP.P3")
        snapshot["loops"][1]["no_material_change_streak"] = 3
        snapshot["advisor_proposals"] = [
            {
                "proposal_id": "P.SECOND",
                "loop_id": "LOOP.P3",
                "action": "SET_CADENCE",
                "target_cadence_seconds": 259200,
                "reason": "second proposal",
                "confidence": 1.0,
                "evidence_refs": ["EV.P3"],
            }
        ]
        result = AOG.evaluate(snapshot)
        self.assertEqual(1, len(result["mutation_intents"]))
        rejected = [item for item in result["adaptation_proposals"] if "CANARY_MUTATION_LIMIT_REACHED" in item["reasons"]]
        self.assertEqual(1, len(rejected))

    def test_restart_with_same_snapshot_is_deterministic(self) -> None:
        snapshot = make_snapshot()
        snapshot["loops"][1]["no_material_change_streak"] = 3
        first = AOG.evaluate(copy.deepcopy(snapshot))
        second = AOG.evaluate(copy.deepcopy(snapshot))
        self.assertEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(first, second)


class OutcomeAndCliTests(unittest.TestCase):
    def test_only_three_verified_classes_count_as_value(self) -> None:
        snapshot = make_snapshot()
        classes = [
            "VERIFIED_CONSEQUENCE",
            "VERIFIED_INFORMATION_GAIN",
            "VERIFIED_ABSTENTION",
            "NO_MATERIAL_CHANGE",
            "BLOCKED",
            "FAILED",
            "CONTRADICTED",
        ]
        snapshot["recent_outcomes"] = [
            {
                "outcome_id": f"OUT.{index}",
                "loop_id": "LOOP.P3",
                "outcome_class": name,
                "observed_at": snapshot["snapshot_at"],
                "evidence_refs": [f"EV.OUT.{index}"] if name.startswith("VERIFIED_") else [],
            }
            for index, name in enumerate(classes)
        ]
        result = AOG.evaluate(snapshot)
        self.assertEqual(3, result["metrics"]["verified_value_outcomes"])
        self.assertEqual(7, result["metrics"]["window_outcomes"])

    def test_cli_returns_three_for_fail_closed_canary(self) -> None:
        snapshot = make_snapshot(requested_mode="CANARY")
        add_canary_authority(snapshot)
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "snapshot.json"
            source.write_text(json.dumps(snapshot), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(MODULE_PATH), str(source)],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(3, proc.returncode)
        parsed = json.loads(proc.stdout)
        self.assertEqual("SHADOW", parsed["effective_mode"])

    def test_cli_policy_hash_matches_module(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--print-policy-hash"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, proc.returncode)
        self.assertEqual(AOG.POLICY_HASH, proc.stdout.strip())


if __name__ == "__main__":
    unittest.main()
