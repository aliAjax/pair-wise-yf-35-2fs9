import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class SanctionCaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------

    def make_athlete(self, name="A. Rider"):
        return self.service.create(
            self.actor, "athlete", {"name": name, "discipline": "cycling"}
        )["id"]

    def make_case(self, athlete_id):
        sample = self.service.create(
            self.actor, "sample",
            {"athlete_id": athlete_id, "sample_code": "S-" + athlete_id[:6],
             "event": "event"},
        )
        for action, data in [
            ("collect", {"collected_at": "2024-01-01T08:00"}),
            ("seal", {"seal_id": "seal"}),
            ("ship", {"carrier": "c"}),
            ("receive", {"lab_id": "lab"}),
            ("analyze", {"result": "adverse"}),
        ]:
            sample = self.service.transition(self.actor, sample["id"], action, data)
        sample = self.service.transition(
            self.actor, sample["id"], "report_adverse", {}
        )
        return self.service.create(
            self.actor, "case",
            {"athlete_id": athlete_id, "sample_id": sample["id"],
             "alleged_rule": "r1"},
        )

    def decide(self, case, months, decision_date, *, action="decide"):
        case = self.service.transition(
            self.actor, case["id"], "schedule_hearing",
            {"hearing_at": decision_date},
        )
        return self.service.transition(
            self.actor, case["id"], action,
            {"decision": "sanction", "months": months,
             "decision_date": decision_date},
        )

    def provisionally_suspend(self, case, started):
        return self.service.transition(
            self.actor, case["id"], "provisional_suspend",
            {"reason": "adverse", "started_at": started},
        )

    def active_sanctions(self, case_id):
        return [
            s for s in self.repo.find_entities("sanction", "case_id", case_id)
            if s["status"] == "active"
        ]

    def current_sanction(self, case_id):
        active = self.active_sanctions(case_id)
        self.assertEqual(len(active), 1)
        return active[0]

    # -- sanction period is generated from months ----------------------

    def test_decide_generates_months_based_period(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.decide(case, 24, "2025-03-01")
        sanction = self.current_sanction(case["id"])
        self.assertEqual(sanction["data"]["months"], 24)
        self.assertEqual(sanction["data"]["raw_end"], "2027-03-01")
        self.assertEqual(sanction["data"]["adjusted_end"], "2027-03-01")
        self.assertEqual(sanction["data"]["credited_days"], 0)

    def test_decide_without_months_rejected(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        case = self.service.transition(
            self.actor, case["id"], "schedule_hearing", {"hearing_at": "2025-03-01"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, case["id"], "decide",
                {"decision": "sanction", "decision_date": "2025-03-01"},
            )

    # -- provisional suspension credit ----------------------------------

    def test_continuous_unrevoked_provisional_is_credited(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.provisionally_suspend(case, "2025-01-01")
        self.decide(case, 24, "2025-03-01")
        sanction = self.current_sanction(case["id"])
        # Jan 1 .. Feb 28 inclusive = 59 days, decision day not credited
        self.assertEqual(sanction["data"]["credited_days"], 59)
        self.assertEqual(sanction["data"]["adjusted_end"], "2027-01-01")

    def test_revoked_provisional_is_not_credited(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.provisionally_suspend(case, "2025-01-01")
        self.service.transition(
            self.actor, case["id"], "lift_provisional", {"reason": "cleared"}
        )
        self.provisionally_suspend(case, "2025-02-10")
        self.decide(case, 24, "2025-03-01")
        sanction = self.current_sanction(case["id"])
        # Only Feb 10 .. Feb 28 counts; the revoked January run is dropped.
        self.assertEqual(sanction["data"]["credited_days"], 19)
        ranges = sanction["data"]["credit_ranges"]
        self.assertEqual([r["start"] for r in ranges], ["2025-02-10"])

    def test_gap_splits_credit_but_both_runs_count(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        # Two un-revoked provisional runs with a gap between them.
        with self.repo.transaction() as store:
            store.create_entity(
                "prov-run-1", "provisional_suspension", "in_force",
                {"athlete_id": athlete, "case_id": case["id"],
                 "reason": "r", "started_at": "2025-01-01",
                 "ended_at": "2025-01-20"},
                "admin",
            )
            store.create_entity(
                "prov-run-2", "provisional_suspension", "in_force",
                {"athlete_id": athlete, "case_id": case["id"],
                 "reason": "r", "started_at": "2025-02-15",
                 "ended_at": "2025-02-28"},
                "admin",
            )
        self.decide(case, 24, "2025-03-01")
        sanction = self.current_sanction(case["id"])
        # Both runs credited but the gap (Jan 21 .. Feb 14) is not.
        self.assertEqual(sanction["data"]["credited_days"], 20 + 14)
        starts = [r["start"] for r in sanction["data"]["credit_ranges"]]
        self.assertEqual(starts, ["2025-01-01", "2025-02-15"])

    def test_overlapping_cases_do_not_double_credit(self):
        athlete = self.make_athlete()
        case1, case2 = self.make_case(athlete), self.make_case(athlete)
        # case 1 provisional: Jan 1 .. Mar 1
        self.provisionally_suspend(case1, "2025-01-01")
        # case 2 provisional overlaps: Feb 1 .. Mar 1
        self.provisionally_suspend(case2, "2025-02-01")
        self.decide(case1, 24, "2025-03-01")
        s1 = self.current_sanction(case1["id"])
        self.assertEqual(s1["data"]["credited_days"], 59)  # Jan1..Feb28
        self.decide(case2, 12, "2025-03-01")
        s2 = self.current_sanction(case2["id"])
        # case 2 own provisional is Feb 1..28, all already credited by case 1
        self.assertEqual(s2["data"]["credited_days"], 0)
        self.assertEqual(s2["data"]["adjusted_end"], "2026-03-01")

    # -- appeals keep old version and annulled results ------------------

    def test_appeal_revision_supersedes_and_keeps_old(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.provisionally_suspend(case, "2025-01-01")
        self.decide(case, 24, "2025-03-01")
        old = self.current_sanction(case["id"])

        case = self.repo.get_entity(case["id"])
        self.service.transition(self.actor, case["id"], "appeal", {"grounds": "g"})
        self.service.transition(
            self.actor, case["id"], "resolve_appeal",
            {"decision": "sanction", "months": 12, "decision_date": "2025-03-01"},
        )
        old = self.repo.get_entity(old["id"])
        self.assertEqual(old["status"], "superseded")
        new = self.current_sanction(case["id"])
        self.assertEqual(new["data"]["revision"], 2)
        self.assertEqual(new["data"]["supersedes"], [old["id"]])
        self.assertEqual(new["data"]["months"], 12)
        self.assertEqual(old["data"]["adjusted_end"], "2027-01-01")

    def test_appeal_no_sanction_keeps_old_version_and_annulments(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.provisionally_suspend(case, "2025-01-01")
        self.decide(case, 24, "2025-03-01")
        old = self.current_sanction(case["id"])

        # Historical result inside the credited provisional window: register
        # it before the decision is impossible through the API, so simulate a
        # late import then re-run annulment by appealing and re-deciding.
        with self.repo.transaction() as store:
            result = store.create_entity(
                "r-import-1", "competition_result", "registered",
                {"athlete_id": athlete, "event": "Imported",
                 "event_date": "2025-02-10", "placing": "1st",
                 "annulled": False},
                "importer",
            )

        case = self.repo.get_entity(case["id"])
        self.service.transition(self.actor, case["id"], "appeal", {"grounds": "g"})
        self.service.transition(
            self.actor, case["id"], "resolve_appeal",
            {"decision": "sanction", "months": 24, "decision_date": "2025-03-01"},
        )
        new = self.current_sanction(case["id"])
        self.assertEqual(
            self.repo.get_entity("r-import-1")["status"], "annulled"
        )
        annulled_ids = {a["result_id"] for a in new["data"]["annulled_results"]}
        self.assertIn("r-import-1", annulled_ids)

        # Now vindicated on a second appeal: periods disappear from eligibility
        # but the old version and the annulled result are both retained.
        case = self.repo.get_entity(case["id"])
        self.service.transition(self.actor, case["id"], "appeal", {"grounds": "g2"})
        self.service.transition(
            self.actor, case["id"], "resolve_appeal",
            {"decision": "no_sanction", "decision_date": "2025-06-01"},
        )
        self.assertEqual(self.repo.get_entity(old["id"])["status"], "superseded")
        self.assertEqual(
            self.repo.get_entity(new["id"])["status"], "superseded"
        )
        self.assertEqual(
            self.repo.get_entity("r-import-1")["status"], "annulled"
        )
        view = self.service.athlete_eligibility(athlete, "2026-01-01")
        self.assertTrue(view["eligible"])

    # -- result registration reads the periods in force ----------------

    def test_result_during_blocked_window_is_rejected_and_kept(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.decide(case, 24, "2025-01-01")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.actor, "competition_result",
                {"athlete_id": athlete, "event": "Race",
                 "event_date": "2025-06-01", "placing": "1st"},
            )
        kept = self.repo.find_entities(
            "competition_result", "athlete_id", athlete
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["status"], "rejected")
        self.assertEqual(kept[0]["data"]["placing"], "1st")

    def test_result_before_sanction_stays_registered(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.decide(case, 24, "2025-03-01")
        result = self.service.create(
            self.actor, "competition_result",
            {"athlete_id": athlete, "event": "Race",
             "event_date": "2025-01-10", "placing": "2nd"},
        )
        self.assertEqual(result["status"], "registered")

    def test_deciding_annuls_historical_results_and_keeps_clean_ones(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.provisionally_suspend(case, "2025-01-01")
        with self.repo.transaction() as store:
            inside = store.create_entity(
                "r-in", "competition_result", "registered",
                {"athlete_id": athlete, "event": "During",
                 "event_date": "2025-02-01", "placing": "1st",
                 "annulled": False},
                "importer",
            )
            outside = store.create_entity(
                "r-out", "competition_result", "registered",
                {"athlete_id": athlete, "event": "Before",
                 "event_date": "2024-12-01", "placing": "3rd",
                 "annulled": False},
                "importer",
            )
        self.decide(case, 24, "2025-03-01")
        self.assertEqual(self.repo.get_entity("r-in")["status"], "annulled")
        self.assertEqual(self.repo.get_entity("r-out")["status"], "registered")
        sanction = self.current_sanction(case["id"])
        annulled = {a["result_id"] for a in sanction["data"]["annulled_results"]}
        self.assertEqual(annulled, {"r-in"})

    # -- comeback tests --------------------------------------------------

    def test_three_negative_tests_after_period_restore_eligibility(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.decide(case, 12, "2025-01-01")  # ends 2026-01-01
        view = self.service.athlete_eligibility(athlete, "2026-02-01")
        self.assertFalse(view["eligible"])
        self.assertEqual(view["reason"], "comeback_tests_incomplete")
        for day in ("2026-01-05", "2026-01-10", "2026-01-15"):
            self.service.create(
                self.actor, "comeback_test",
                {"athlete_id": athlete, "test_date": day, "result": "negative"},
            )
        view = self.service.athlete_eligibility(athlete, "2026-02-01")
        self.assertTrue(view["eligible"])
        self.assertEqual(view["reason"], "reinstated")

    def test_test_on_last_served_day_does_not_count(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.decide(case, 12, "2025-01-01")  # adjusted end 2026-01-01
        for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
            self.service.create(
                self.actor, "comeback_test",
                {"athlete_id": athlete, "test_date": day, "result": "negative"},
            )
        view = self.service.athlete_eligibility(athlete, "2026-02-01")
        self.assertFalse(view["eligible"])
        self.assertEqual(view["detail"]["completed"], 2)

    # -- legacy cases ----------------------------------------------------

    def test_legacy_decided_case_becomes_pending_backfill_and_blocks(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        # Create a raw "old style" closed sanction decision directly.
        case = self.service.transition(
            self.actor, case["id"], "schedule_hearing", {"hearing_at": "2024-01-01"}
        )
        from src.repository import Store
        with self.repo.transaction() as store:
            store.update_entity(
                case["id"], case["version"], "closed",
                {**case["data"], "decision": "sanction",
                 "decision_date": "2024-01-01", "decided_by": "admin"},
            )
        # Reopen repository to trigger migration.
        service2 = DomainService(
            SQLiteRepository(self.repo.path), RuleEngine()
        )
        pending = service2.list("sanctions", status="pending_backfill")
        self.assertEqual(len(pending), 1)
        view = service2.athlete_eligibility(athlete, "2026-01-01")
        self.assertFalse(view["eligible"])
        self.assertEqual(view["reason"], "pending_backfill")

    def test_backfill_runs_same_checks_and_unblocks(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        case = self.service.transition(
            self.actor, case["id"], "schedule_hearing", {"hearing_at": "2024-01-01"}
        )
        with self.repo.transaction() as store:
            store.update_entity(
                case["id"], case["version"], "closed",
                {**case["data"], "decision": "sanction",
                 "decision_date": "2024-01-01", "decided_by": "admin"},
            )
        service2 = DomainService(SQLiteRepository(self.repo.path), RuleEngine())
        pending = service2.list("sanctions", status="pending_backfill")[0]
        filled = service2.transition(
            self.actor, pending["id"], "backfill", {"months": 1}
        )
        self.assertEqual(filled["status"], "active")
        self.assertEqual(filled["data"]["raw_end"], "2024-02-01")
        view = service2.athlete_eligibility(athlete, "2026-01-01")
        # period long over; without comeback tests the tests-incomplete state
        # only applies after latest blocked day -> needs 3 tests
        self.assertFalse(view["eligible"])
        self.assertEqual(view["reason"], "comeback_tests_incomplete")

    def test_backfill_requires_positive_months(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        case = self.service.transition(
            self.actor, case["id"], "schedule_hearing", {"hearing_at": "2024-01-01"}
        )
        with self.repo.transaction() as store:
            store.update_entity(
                case["id"], case["version"], "closed",
                {**case["data"], "decision": "sanction",
                 "decision_date": "2024-01-01", "decided_by": "admin"},
            )
        service2 = DomainService(SQLiteRepository(self.repo.path), RuleEngine())
        pending = service2.list("sanctions", status="pending_backfill")[0]
        with self.assertRaises(ValidationError):
            service2.transition(
                self.actor, pending["id"], "backfill", {"months": 0}
            )

    def _seed_legacy_sanction_case(self, athlete_id, started, decision_date):
        case = self.make_case(athlete_id)
        with self.repo.transaction() as store:
            store.create_entity(
                "prov-legacy", "provisional_suspension", "in_force",
                {"athlete_id": athlete_id, "case_id": case["id"],
                 "reason": "r", "started_at": started,
                 "ended_at": decision_date},
                "admin",
            )
        case = self.service.transition(
            self.actor, case["id"], "schedule_hearing", {"hearing_at": decision_date}
        )
        with self.repo.transaction() as store:
            store.update_entity(
                case["id"], case["version"], "closed",
                {**case["data"], "decision": "sanction",
                 "decision_date": decision_date, "decided_by": "admin"},
            )
        return DomainService(SQLiteRepository(self.repo.path), RuleEngine())

    def test_pending_backfill_blocks_result_registration(self):
        athlete = self.make_athlete()
        service2 = self._seed_legacy_sanction_case(
            athlete, "2024-01-01", "2024-03-01"
        )
        with self.assertRaises(ValidationError):
            service2.create(
                self.actor, "competition_result",
                {"athlete_id": athlete, "event": "Race",
                 "event_date": "2026-01-01", "placing": "1st"},
            )
        kept = service2.list("competition_results", status="rejected")
        self.assertEqual(len(kept), 1)
        self.assertEqual(
            kept[0]["data"]["eligibility_check"]["reason"], "pending_backfill"
        )

    def test_backfill_credits_provisional_just_like_fresh_decision(self):
        athlete = self.make_athlete()
        service2 = self._seed_legacy_sanction_case(
            athlete, "2024-01-01", "2024-03-01"
        )
        pending = service2.list("sanctions", status="pending_backfill")[0]
        filled = service2.transition(
            self.actor, pending["id"], "backfill", {"months": 12}
        )
        self.assertEqual(filled["status"], "active")
        # Jan 1 .. Feb 29 (2024 leap year) credited, Mar 1 is the sanction day
        self.assertEqual(filled["data"]["credited_days"], 60)
        self.assertEqual(filled["data"]["raw_end"], "2025-03-01")
        self.assertEqual(filled["data"]["adjusted_end"], "2024-12-31")

    def test_shorter_revision_shortens_period_and_three_tests_restore(self):
        athlete = self.make_athlete()
        case = self.make_case(athlete)
        self.decide(case, 24, "2025-01-01")
        case = self.repo.get_entity(case["id"])
        self.service.transition(self.actor, case["id"], "appeal", {"grounds": "g"})
        self.service.transition(
            self.actor, case["id"], "resolve_appeal",
            {"decision": "sanction", "months": 2, "decision_date": "2025-01-01"},
        )
        new = self.current_sanction(case["id"])
        self.assertEqual(new["data"]["adjusted_end"], "2025-03-01")
        # Still blocked on the old 24-month end date? No: revision governs.
        view = self.service.athlete_eligibility(athlete, "2025-06-01")
        self.assertFalse(view["eligible"])
        self.assertEqual(view["reason"], "comeback_tests_incomplete")
        for day in ("2025-03-02", "2025-03-03", "2025-03-04"):
            self.service.create(
                self.actor, "comeback_test",
                {"athlete_id": athlete, "test_date": day, "result": "negative"},
            )
        view = self.service.athlete_eligibility(athlete, "2025-06-01")
        self.assertTrue(view["eligible"])
        self.assertEqual(view["reason"], "reinstated")


if __name__ == "__main__":
    unittest.main()
