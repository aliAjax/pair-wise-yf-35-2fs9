import threading
import unittest
import tempfile
from datetime import date, timedelta
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import REENTRY_TESTS_REQUIRED, RuleEngine, add_months
from src.service import DomainService

ADMIN = Actor("admin", "admin")
PANEL = Actor("panel-1", "panel")
INSPECTOR = Actor("insp-1", "inspector")


class SanctionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.athlete = self.service.create(
            ADMIN, "athlete", {"name": "A. Rider", "discipline": "cycling"}
        )
        self.aid = self.athlete["id"]

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _adverse_sample(self, code="S-1"):
        sample = self.service.create(
            INSPECTOR, "sample",
            {"athlete_id": self.aid, "sample_code": code, "event": "national"},
        )
        for action, data in [
            ("collect", {"collected_at": "2026-01-01T08:00:00Z"}),
            ("seal", {"seal_id": "SEAL-1"}),
            ("ship", {"carrier": "Courier"}),
            ("receive", {"lab_id": "LAB-1"}),
            ("analyze", {"result": "adverse"}),
            ("report_adverse", {}),
        ]:
            sample = self.service.transition(ADMIN, sample["id"], action, data)
        return sample

    def _open_case(self, code="S-1"):
        sample = self._adverse_sample(code)
        return self.service.create(
            PANEL, "case",
            {"athlete_id": self.aid, "sample_id": sample["id"], "alleged_rule": "2.1"},
        )

    def _decide_sanction(
        self,
        case,
        months=12,
        decided_at="2026-03-01",
        provisional_start=None,
    ):
        if provisional_start:
            self.service.transition(
                ADMIN, case["id"], "provisional_suspend",
                {"reason": "adverse", "started_at": provisional_start},
            )
        self.service.transition(
            ADMIN, case["id"], "schedule_hearing", {"hearing_at": "2026-02-20"}
        )
        return self.service.transition(
            PANEL, case["id"], "decide",
            {"decision": "sanction", "suspension_months": months, "decided_at": decided_at},
        )

    def _sanctions(self, case_id, status=None):
        rows = [
            s for s in self.service.list("sanction")
            if s["data"].get("case_id") == case_id
        ]
        if status:
            rows = [s for s in rows if s["status"] == status]
        return rows

    def _eligibility(self, as_of):
        return self.service.eligibility(self.aid, as_of=as_of)

    # ------------------------------------------------------------------
    # 1. Sanction period generation
    # ------------------------------------------------------------------

    def test_decision_generates_sanction_period_from_months(self):
        case = self._open_case()
        case = self._decide_sanction(case, months=12, decided_at="2026-03-01")
        active = self._sanctions(case["id"], "active")
        self.assertEqual(len(active), 1)
        period = active[0]["data"]
        self.assertEqual(period["start_date"], "2026-03-01")
        self.assertEqual(period["raw_end_date"], "2027-03-01")
        self.assertEqual(period["end_date"], "2027-03-01")
        self.assertEqual(period["suspension_months"], 12)
        self.assertEqual(period["version_no"], 1)

    def test_add_months_clamps_end_of_month(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))

    def test_sanction_periods_cannot_be_created_directly(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                ADMIN, "sanction",
                {"athlete_id": self.aid, "suspension_months": 12},
            )

    # ------------------------------------------------------------------
    # 2. Provisional suspension credit
    # ------------------------------------------------------------------

    def test_consecutive_non_revoked_provisional_suspension_is_credited(self):
        case = self._open_case()
        case = self._decide_sanction(
            case, months=12, decided_at="2026-03-01", provisional_start="2026-01-10"
        )
        period = self._sanctions(case["id"], "active")[0]["data"]
        # 2026-01-10 .. 2026-03-01 inclusive = 51 days
        self.assertEqual(period["credits"][0]["served_days"], 51)
        self.assertEqual(period["credited_days"], 51)
        self.assertEqual(period["end_date"], "2027-01-09")

    def test_revoked_segment_is_not_credited(self):
        case = self._open_case()
        self.service.transition(
            ADMIN, case["id"], "provisional_suspend",
            {"reason": "first", "started_at": "2026-01-01"},
        )
        self.service.transition(
            ADMIN, case["id"], "revoke_provisional", {"reason": "cleared"}
        )
        # Re-suspended later: only the consecutive, never-revoked tail counts.
        self.service.transition(
            ADMIN, case["id"], "provisional_suspend",
            {"reason": "again", "started_at": "2026-02-10"},
        )
        self.service.transition(
            ADMIN, case["id"], "schedule_hearing", {"hearing_at": "2026-02-25"}
        )
        case = self.service.transition(
            PANEL, case["id"], "decide",
            {"decision": "sanction", "suspension_months": 6, "decided_at": "2026-03-01"},
        )
        period = self._sanctions(case["id"], "active")[0]["data"]
        # 2026-02-10 .. 2026-03-01 inclusive = 20 days; January segment revoked.
        self.assertEqual(period["credits"][0]["served_days"], 20)

    def test_overlapping_cases_do_not_double_credit(self):
        # Case 1 provisional: 2026-01-10 .. decision 2026-03-01.
        first = self._open_case("S-1")
        first = self._decide_sanction(
            first, months=12, decided_at="2026-03-01",
            provisional_start="2026-01-10",
        )
        # Case 2 provisional falls entirely inside case 1's credited window.
        second = self._open_case("S-2")
        second = self._decide_sanction(
            second, months=12, decided_at="2026-03-01",
            provisional_start="2026-02-01",
        )
        p1 = self._sanctions(first["id"], "active")[0]["data"]
        p2 = self._sanctions(second["id"], "active")[0]["data"]
        self.assertEqual(p2["credits"][0]["served_days"], 29)
        self.assertEqual(p2["credits"][0]["overlap_days"], 29)
        self.assertEqual(p2["credits"][0]["credited_days"], 0)
        self.assertEqual(p2["end_date"], p2["raw_end_date"])
        # Case 1 kept its full credit.
        self.assertEqual(p1["credited_days"], 51)

    # ------------------------------------------------------------------
    # 3. Result registration and voiding
    # ------------------------------------------------------------------

    def test_result_during_effective_period_is_rejected_and_keeps_old_results(self):
        case = self._open_case()
        case = self._decide_sanction(
            case, months=12, decided_at="2026-03-01",
            provisional_start="2026-01-10",
        )
        before = len(self.service.list("result"))
        with self.assertRaises(ValidationError):
            self.service.create(
                INSPECTOR, "result",
                {"athlete_id": self.aid, "event_name": "Tour",
                 "event_date": "2026-05-01", "result": "1st"},
            )
        self.assertEqual(len(self.service.list("result")), before)

    def test_existing_result_in_window_is_annulled_but_data_retained(self):
        result = self.service.create(
            INSPECTOR, "result",
            {"athlete_id": self.aid, "event_name": "Criterium",
             "event_date": "2026-02-15", "result": "1st", "notes": "podium"},
        )
        case = self._open_case()
        case = self._decide_sanction(
            case, months=12, decided_at="2026-03-01",
            provisional_start="2026-01-10",
        )
        stored = self.service.get(result["id"])
        self.assertEqual(stored["status"], "annulled")
        # Original result data is preserved.
        self.assertEqual(stored["data"]["result"], "1st")
        self.assertEqual(stored["data"]["notes"], "podium")
        self.assertEqual(len(stored["data"]["annulments"]), 1)
        self.assertEqual(
            self._sanctions(case["id"], "active")[0]["data"]["annulled_results"],
            [result["id"]],
        )

    def test_provisional_suspension_blocks_registration_before_decision(self):
        case = self._open_case()
        self.service.transition(
            ADMIN, case["id"], "provisional_suspend",
            {"reason": "adverse", "started_at": "2026-01-10"},
        )
        self.assertEqual(
            self._eligibility("2026-01-20")["reason"], "provisionally_suspended"
        )
        with self.assertRaises(ValidationError):
            self.service.create(
                INSPECTOR, "result",
                {"athlete_id": self.aid, "event_name": "Race",
                 "event_date": "2026-01-20", "result": "1st"},
            )
        # Revoking the provisional suspension lifts the block until a
        # decision is issued.
        self.service.transition(
            ADMIN, case["id"], "revoke_provisional", {"reason": "cleared"}
        )
        self.assertTrue(self._eligibility("2026-01-20")["eligible"])

    def test_result_before_ineligibility_window_stays_valid(self):
        result = self.service.create(
            INSPECTOR, "result",
            {"athlete_id": self.aid, "event_name": "Old Race",
             "event_date": "2025-12-31", "result": "2nd"},
        )
        case = self._open_case()
        self._decide_sanction(
            case, months=12, decided_at="2026-03-01",
            provisional_start="2026-01-10",
        )
        self.assertEqual(self.service.get(result["id"])["status"], "recorded")

    # ------------------------------------------------------------------
    # 4. Rehearing / revision keeps old versions and annulled results
    # ------------------------------------------------------------------

    def test_appeal_revision_supersedes_old_version_which_is_retained(self):
        result = self.service.create(
            INSPECTOR, "result",
            {"athlete_id": self.aid, "event_name": "Race",
             "event_date": "2026-02-15", "result": "1st"},
        )
        case = self._open_case()
        case = self._decide_sanction(
            case, months=12, decided_at="2026-03-01",
            provisional_start="2026-02-01",
        )
        self.service.transition(ADMIN, case["id"], "appeal", {"grounds": "duration"})
        case = self.service.transition(
            PANEL, case["id"], "resolve_appeal",
            {"decision": "sanction", "suspension_months": 24, "decided_at": "2026-03-01"},
        )
        rows = self._sanctions(case["id"])
        self.assertEqual({s["status"] for s in rows}, {"active", "superseded"})
        active = self._sanctions(case["id"], "active")[0]
        self.assertEqual(active["data"]["version_no"], 2)
        self.assertEqual(active["data"]["suspension_months"], 24)
        superseded = self._sanctions(case["id"], "superseded")[0]
        self.assertEqual(superseded["data"]["version_no"], 1)
        self.assertEqual(superseded["data"]["suspension_months"], 12)
        # Annulled result stays annulled with its original trace (no duplicate).
        stored = self.service.get(result["id"])
        self.assertEqual(stored["status"], "annulled")
        self.assertEqual(len(stored["data"]["annulments"]), 1)

    def test_appeal_to_no_sanction_vacates_period(self):
        case = self._open_case()
        case = self._decide_sanction(case, months=12, decided_at="2026-03-01")
        self.service.transition(ADMIN, case["id"], "appeal", {"grounds": "cleared"})
        self.service.transition(
            PANEL, case["id"], "resolve_appeal",
            {"decision": "no_sanction", "decided_at": "2026-04-01"},
        )
        statuses = {s["status"] for s in self._sanctions(case["id"])}
        self.assertEqual(statuses, {"vacated"})
        decision = self._eligibility("2026-05-01")
        self.assertTrue(decision["eligible"])

    # ------------------------------------------------------------------
    # 5. Comeback tests
    # ------------------------------------------------------------------

    def _end_date(self, case_id):
        return self._sanctions(case_id, "active")[0]["data"]["end_date"]

    def test_three_comeback_tests_required_after_period_ends(self):
        case = self._open_case()
        case = self._decide_sanction(case, months=12, decided_at="2026-03-01")
        end = self._end_date(case["id"])
        after = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        self.assertEqual(
            self._eligibility(end)["reason"], "suspended"
        )
        self.assertEqual(
            self._eligibility(after)["reason"], "awaiting_comeback_tests"
        )
        for i in range(REENTRY_TESTS_REQUIRED - 1):
            self.service.create(
                INSPECTOR, "comeback_test",
                {"athlete_id": self.aid, "test_date": after, "sample_code": "T-%d" % i},
            )
            self.assertFalse(self._eligibility(after)["eligible"])
        self.service.create(
            INSPECTOR, "comeback_test",
            {"athlete_id": self.aid, "test_date": after, "sample_code": "T-2"},
        )
        decision = self._eligibility(after)
        self.assertTrue(decision["eligible"])
        self.assertEqual(len(decision["comeback_tests"]), 3)
        # Result registration after reinstatement works.
        recorded = self.service.create(
            INSPECTOR, "result",
            {"athlete_id": self.aid, "event_name": "Return",
             "event_date": after, "result": "5th"},
        )
        self.assertEqual(recorded["status"], "recorded")

    def test_comeback_test_on_last_day_does_not_count(self):
        case = self._open_case()
        case = self._decide_sanction(case, months=12, decided_at="2026-03-01")
        end = self._end_date(case["id"])
        self.service.create(
            INSPECTOR, "comeback_test",
            {"athlete_id": self.aid, "test_date": end, "sample_code": "ON-END"},
        )
        after = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        decision = self._eligibility(after)
        self.assertEqual(decision["reason"], "awaiting_comeback_tests")
        self.assertEqual(decision["comeback_tests_completed"], 0)

    # ------------------------------------------------------------------
    # 6. Legacy cases missing a sanction period
    # ------------------------------------------------------------------

    def test_legacy_case_gets_pending_period_and_blocks_participation(self):
        case = self._open_case("S-OLD")
        # Simulate an old-version closed case whose decision lived only in notes.
        self.repository_update_closed(case, {"old_decision_note": "禁赛1年（写在备注里）"})
        created = self.service.provision_legacy_cases()
        self.assertEqual(len(created), 1)
        # Idempotent: second run adds nothing.
        self.assertEqual(self.service.provision_legacy_cases(), [])
        pending = self._sanctions(case["id"], "pending_backfill")[0]
        self.assertTrue(pending["data"]["legacy"])
        for as_of in ("2026-06-01", "2030-01-01"):
            self.assertEqual(
                self._eligibility(as_of)["reason"], "pending_backfill"
            )
            with self.assertRaises(ValidationError):
                self.service.create(
                    INSPECTOR, "result",
                    {"athlete_id": self.aid, "event_name": "Race",
                     "event_date": as_of, "result": "1st"},
                )

    def test_legacy_no_sanction_note_is_not_provisioned(self):
        case = self._open_case("S-NOPE")
        self.repository_update_closed(case, {"decision_note": "不予禁赛，警告处理"})
        self.assertEqual(self.service.provision_legacy_cases(), [])
        self.assertEqual(self._sanctions(case["id"]), [])

    def test_backfill_runs_same_credit_checks(self):
        case = self._open_case("S-OLD")
        self.repository_update_closed(case, {"old_decision_note": "禁赛"})
        self.service.provision_legacy_cases()
        pending = self._sanctions(case["id"], "pending_backfill")[0]
        # Attach provisional service recorded before the upgrade.
        current = self.service.get(case["id"])
        self.service.repository.update_entity(
            case["id"], current["version"], "closed",
            {**current["data"], "provisional_segments": [
                {"id": "p1", "start_date": "2026-02-01", "end_date": "2026-03-01"}
            ]},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                PANEL, pending["id"], "backfill",
                {"suspension_months": "not-a-number", "decided_at": "2026-03-01"},
            )
        active = self.service.transition(
            PANEL, pending["id"], "backfill",
            {"suspension_months": 12, "decided_at": "2026-03-01"},
        )
        self.assertEqual(active["status"], "active")
        # 2026-02-01 .. 2026-03-01 = 29 days credited through the same path.
        self.assertEqual(active["data"]["credited_days"], 29)
        self.assertEqual(active["data"]["end_date"], "2027-01-31")

    def test_decision_without_months_becomes_pending_backfill(self):
        case = self._open_case()
        self.service.transition(
            ADMIN, case["id"], "schedule_hearing", {"hearing_at": "2026-02-20"}
        )
        case = self.service.transition(
            PANEL, case["id"], "decide",
            {"decision": "sanction", "decided_at": "2026-03-01"},
        )
        pending = self._sanctions(case["id"], "pending_backfill")
        self.assertEqual(len(pending), 1)
        self.assertFalse(self._eligibility("2026-06-01")["eligible"])

    def repository_update_closed(self, case, extra):
        current = self.service.get(case["id"])
        self.service.repository.update_entity(
            case["id"], current["version"], "closed",
            {**current["data"], **extra},
        )

    # ------------------------------------------------------------------
    # 7. Atomicity: revision and registration arriving together
    # ------------------------------------------------------------------

    def test_concurrent_revision_and_registration_never_disagree(self):
        case = self._open_case()
        case = self._decide_sanction(case, months=12, decided_at="2026-03-01")
        race_day = "2026-06-01"
        # Sanity: before the revision the athlete is banned on race day.
        self.assertFalse(self._eligibility(race_day)["eligible"])
        errors = []
        outcomes = []

        def revise():
            try:
                self.service.transition(
                    ADMIN, case["id"], "appeal", {"grounds": "new evidence"}
                )
                self.service.transition(
                    PANEL, case["id"], "resolve_appeal",
                    {"decision": "no_sanction", "decided_at": "2026-06-01"},
                )
                outcomes.append("revised")
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def register():
            try:
                self.service.create(
                    INSPECTOR, "result",
                    {"athlete_id": self.aid, "event_name": "Race",
                     "event_date": race_day, "result": "1st"},
                )
                outcomes.append("registered")
            except ValidationError:
                outcomes.append("rejected")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        # Repeat several times: BEGIN IMMEDIATE serializes the two writers,
        # so both outcomes must agree with the view taken afterwards.
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                # Restore an active sanction for this attempt.
                if attempt:
                    self.service.transition(
                        ADMIN, case["id"], "appeal",
                        {"grounds": "reopen %d" % attempt},
                    )
                    case = self.service.transition(
                        PANEL, case["id"], "resolve_appeal",
                        {"decision": "sanction", "suspension_months": 12,
                         "decided_at": "2026-03-01"},
                    )
                outcomes.clear()
                barrier = threading.Barrier(2)

                def run(target):
                    barrier.wait()
                    target()

                t1 = threading.Thread(target=run, args=(revise,))
                t2 = threading.Thread(target=run, args=(register,))
                t1.start(); t2.start(); t1.join(); t2.join()
                self.assertEqual(errors, [])
                self.assertIn("revised", outcomes)
                view = self._eligibility(race_day)
                if "registered" in outcomes:
                    # Core invariant: a result can only be committed when the
                    # guard saw eligibility. The committed view must agree.
                    self.assertTrue(
                        view["eligible"],
                        "registration succeeded while eligibility shows a ban",
                    )
                # A rejection followed by the revision committing is a valid
                # serial order (register-then-revise), never a split view.
                results_on_day = [
                    r for r in self.service.list("result")
                    if r["data"].get("event_date") == race_day
                ]
                if results_on_day:
                    self.assertTrue(view["eligible"])


if __name__ == "__main__":
    unittest.main()
