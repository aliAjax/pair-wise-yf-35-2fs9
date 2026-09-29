import threading
import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.repo = SQLiteRepository(self.db_path)
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_decided_case(self, athlete_id, started, decision_date, months):
        sample = self.service.create(
            self.actor, "sample",
            {"athlete_id": athlete_id, "sample_code": "S-X", "event": "e"},
        )
        for action, data in [
            ("collect", {"collected_at": started + "T08:00"}),
            ("seal", {"seal_id": "s"}),
            ("ship", {"carrier": "c"}),
            ("receive", {"lab_id": "l"}),
            ("analyze", {"result": "adverse"}),
        ]:
            sample = self.service.transition(self.actor, sample["id"], action, data)
        sample = self.service.transition(self.actor, sample["id"], "report_adverse", {})
        case = self.service.create(
            self.actor, "case",
            {"athlete_id": athlete_id, "sample_id": sample["id"], "alleged_rule": "r"},
        )
        self.service.transition(
            self.actor, case["id"], "provisional_suspend",
            {"reason": "r", "started_at": started},
        )
        case = self.service.transition(
            self.actor, case["id"], "schedule_hearing", {"hearing_at": decision_date}
        )
        case = self.service.transition(
            self.actor, case["id"], "decide",
            {"decision": "sanction", "months": months,
             "decision_date": decision_date},
        )
        return case

    def test_appeal_resolution_and_result_registration_serialize(self):
        """Concurrent redecision/result registration must never produce a
        view where the athlete is eligible for racing but still suspended."""
        athlete = self.service.create(
            self.actor, "athlete", {"name": "Racer", "discipline": "cycling"}
        )["id"]
        # 24 months decided 2025-03-01: Feb 2026 is blocked.
        case = self._make_decided_case(athlete, "2025-01-01", "2025-03-01", 24)
        case_id = case["id"]

        barrier = threading.Barrier(2)
        errors = []

        def appeal():
            barrier.wait()
            local = DomainService(SQLiteRepository(self.db_path), RuleEngine())
            try:
                c = local.transition(Actor("admin", "admin"), case_id, "appeal",
                                     {"grounds": "g"})
                local.transition(
                    Actor("admin", "admin"), c["id"], "resolve_appeal",
                    {"decision": "no_sanction", "decision_date": "2026-09-01"},
                )
            except Exception as exc:  # serialization loser may retry elsewhere
                errors.append(("appeal", type(exc).__name__))

        def register():
            barrier.wait()
            local = DomainService(SQLiteRepository(self.db_path), RuleEngine())
            try:
                local.create(
                    Actor("admin", "admin"), "competition_result",
                    {"athlete_id": athlete, "event": "Race",
                     "event_date": "2026-02-15", "placing": "1st"},
                )
            except ValidationError:
                pass  # expected when the sanction was still in force
            except Exception as exc:
                errors.append(("register", type(exc).__name__))

        threads = [threading.Thread(target=appeal), threading.Thread(target=register)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The loser may see the already-committed new state and fail the
        # transition; only a durable serialization error is acceptable.
        unexpected = [
            "%s:%s" % (op, kind)
            for op, kind in errors
            if kind not in ("ConflictError", "InvalidTransition")
        ]
        self.assertEqual(unexpected, [])

        # The single durable state must be internally consistent:
        # if the result was accepted, no active sanction may block that date;
        # if an active sanction blocks it, the result had to be rejected.
        final = self.service.athlete_eligibility(athlete, "2026-02-15")
        results = self.repo.find_entities(
            "competition_result", "athlete_id", athlete
        )
        registered = [r for r in results if r["status"] == "registered"]
        if final["eligible"]:
            # vindication won: registering earlier may have failed under the
            # old state, that's fine; but no registered result may coexist
            # with an active blocking sanction.
            blocking = [
                s for s in self.repo.find_entities("sanction", "athlete_id", athlete)
                if s["status"] == "active"
            ]
            self.assertEqual(blocking, [])
        else:
            self.assertFalse(registered)
            self.assertEqual(final["reason"], "suspended")

    def test_concurrent_decisions_keep_one_active_version(self):
        athlete = self.service.create(
            self.actor, "athlete", {"name": "R2", "discipline": "cycling"}
        )["id"]
        case = self._make_decided_case(athlete, "2025-01-01", "2025-03-01", 24)
        case_id = case["id"]
        self.service.transition(self.actor, case_id, "appeal", {"grounds": "g"})
        barrier = threading.Barrier(2)
        outcomes = []

        def resolve(months):
            barrier.wait()
            local = DomainService(SQLiteRepository(self.db_path), RuleEngine())
            try:
                local.transition(
                    Actor("admin", "admin"), case_id, "resolve_appeal",
                    {"decision": "sanction", "months": months,
                     "decision_date": "2026-09-01"},
                )
                outcomes.append("won")
            except Exception:
                outcomes.append("lost")

        threads = [threading.Thread(target=resolve, args=(m,)) for m in (6, 12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        active = [
            s for s in self.repo.find_entities("sanction", "case_id", case_id)
            if s["status"] == "active"
        ]
        self.assertEqual(len(active), 1)
        self.assertEqual(outcomes.count("won"), 1)
        self.assertEqual(outcomes.count("lost"), 1)
        self.assertIn(active[0]["data"]["months"], (6, 12))


if __name__ == "__main__":
    unittest.main()
