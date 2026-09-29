from datetime import date
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    ValidationError,
)
from .repository import utcnow
from .rules import (
    RuleEngine,
    as_date,
    build_sanction_period,
    evaluate_eligibility,
)

SYSTEM_ACTOR_ID = "system"

class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    # ------------------------------------------------------------------
    # Lookup / health
    # ------------------------------------------------------------------

    def _lookup(self, kind, field, value, conn=None):
        return self.repository.find_entities(
            self.rules.normalize_kind(kind), field, value, conn=conn
        )

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        with self.repository.transaction() as conn:
            if idempotency_key:
                existing = self.repository.get_idempotency(
                    actor.user_id, idempotency_key, conn=conn
                )
                if existing:
                    entity = self.repository.get_entity(existing, conn=conn)
                    if entity:
                        return entity
            # Old decided cases without a sanction period must block result
            # registration; provisioning runs inside the same transaction so
            # the guard always sees them.
            if kind == "result":
                self.provision_legacy_cases(actor, conn=conn)
            self.rules.validate_create(actor, kind, payload, self._tx_lookup(conn))
            if kind == "result":
                self._guard_result_registration(payload, conn)
            entity_id = str(payload.pop("id", "") or uuid4())
            if self.repository.get_entity(entity_id, conn=conn):
                raise ConflictError("entity already exists: " + entity_id)
            status = self.rules.initial_status(kind)
            entity = self.repository.create_entity(
                entity_id, kind, status, payload, actor.user_id, conn=conn
            )
            self.repository.append_audit(
                entity_id, actor.user_id, actor.role, "create", None, status,
                {"kind": kind}, conn=conn,
            )
            if idempotency_key:
                self.repository.save_idempotency(
                    actor.user_id, idempotency_key, entity_id, conn=conn
                )
        return self.repository.get_entity(entity_id)

    def _tx_lookup(self, conn):
        def lookup(kind, field, value):
            return self._lookup(kind, field, value, conn=conn)
        return lookup

    def _guard_result_registration(self, payload, conn):
        """Read the sanction periods effective at the competition date.
        A hit rejects the registration; existing results stay untouched."""
        athlete_id = payload["athlete_id"]
        event_date = as_date(payload["event_date"]).isoformat()
        sanctions = self.repository.list_entities(kind="sanction", conn=conn)
        tests = self.repository.list_entities(kind="comeback_test", conn=conn)
        cases = self.repository.list_entities(kind="case", conn=conn)
        decision = evaluate_eligibility(
            athlete_id, event_date, sanctions, tests, cases
        )
        if not decision["eligible"]:
            raise ValidationError(
                "result rejected: athlete not eligible on %s (%s)"
                % (event_date, decision["reason"])
            )

    # ------------------------------------------------------------------
    # Transition
    # ------------------------------------------------------------------

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        with self.repository.transaction() as conn:
            entity = self.repository.get_entity(entity_id, conn=conn)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            kind = self.rules.normalize_kind(entity["kind"])
            payload = dict(data or {})
            self._apply_defaults(kind, action, payload)
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, payload, self._tx_lookup(conn)
            )
            merged = dict(entity["data"])
            merged.update(patch)

            if kind == "case":
                # Provisional segments must be closed with the merged data
                # before the case row is persisted, so the decision sees the
                # full served interval up to the decision date.
                if action in ("decide", "resolve_appeal"):
                    self._close_provisional_segments(merged, patch["decided_at"])
                self._handle_case_hook(conn, actor, entity, action, patch, merged)
            elif kind == "sanction" and action == "backfill":
                self._activate_backfill(conn, actor, entity, patch)
                return self.repository.get_entity(entity_id, conn=conn)

            updated = self.repository.update_entity(
                entity_id,
                int(expected_version) if expected_version is not None else entity["version"],
                next_status,
                merged,
                conn=conn,
            )
            self.repository.append_audit(
                entity_id, actor.user_id, actor.role, action,
                entity["status"], updated["status"], {"patch": patch}, conn=conn,
            )
        return self.repository.get_entity(entity_id)

    @staticmethod
    def _apply_defaults(kind, action, payload):
        today = date.today().isoformat()
        if kind == "case":
            if action == "provisional_suspend" and not payload.get("started_at"):
                payload["started_at"] = today
            elif action in ("decide", "resolve_appeal") and not payload.get("decided_at"):
                payload["decided_at"] = today
        elif kind == "sanction" and action == "backfill" and not payload.get("decided_at"):
            payload["decided_at"] = today

    # ------------------------------------------------------------------
    # Case hooks: provisional suspension bookkeeping and decisions
    # ------------------------------------------------------------------

    def _handle_case_hook(self, conn, actor, case_entity, action, patch, merged):
        if action == "provisional_suspend":
            start_date = as_date(patch["started_at"]).isoformat()
            segments = merged.setdefault("provisional_segments", [])
            segments.append({
                "id": str(uuid4())[:8],
                "start_date": start_date,
                "end_date": None,
                "revoked_date": None,
                "reason": patch.get("reason", ""),
                "started_by": actor.user_id,
            })
            merged["provisional_suspended_at"] = start_date
        elif action == "revoke_provisional":
            segments = merged.setdefault("provisional_segments", [])
            open_segments = [s for s in segments if not s["end_date"] and not s["revoked_date"]]
            if not open_segments:
                raise InvalidTransition("no active provisional suspension to revoke")
            today = date.today().isoformat()
            for segment in open_segments:
                segment["revoked_date"] = today
                segment["revoke_reason"] = patch.get("reason", "")
                segment["revoked_by"] = actor.user_id
        elif action in ("decide", "resolve_appeal"):
            self._record_decision(merged, action, patch)
            # Sanction period is generated (or revised) from the decision,
            # in the same transaction as the case status change.
            self._apply_decision_to_sanctions(
                conn, actor, case_entity, merged, patch
            )

    @staticmethod
    def _close_provisional_segments(merged, decided_at):
        decided = as_date(decided_at).isoformat()
        for segment in merged.get("provisional_segments", []):
            if not segment["end_date"] and not segment["revoked_date"]:
                segment["end_date"] = decided

    @staticmethod
    def _record_decision(merged, action, patch):
        history = merged.setdefault("decisions", [])
        history.append({
            "stage": "appeal" if action == "resolve_appeal" else "first",
            "decision": patch["decision"],
            "suspension_months": patch.get("suspension_months"),
            "decided_at": patch["decided_at"],
            "decided_by": patch.get("decided_by"),
            "recorded_at": utcnow(),
        })

    def _apply_decision_to_sanctions(self, conn, actor, old_case, merged_case, patch):
        case_id = old_case["id"]
        existing = self.repository.find_entities(
            "sanction", "case_id", case_id, conn=conn
        )
        effective = [
            s for s in existing
            if s["status"] in ("active", "pending_backfill")
        ]
        if patch["decision"] == "sanction":
            months = patch.get("suspension_months")
            if months in (None, ""):
                # Decision carries no suspension length yet: show as pending
                # backfill and block participation until补录.
                self._issue_pending_sanction(
                    conn, actor, old_case, patch, effective
                )
            else:
                self._issue_active_sanction(
                    conn, actor, old_case, merged_case, patch, effective
                )
        else:
            # Reversed to no sanction: vacate previous period, keep versions.
            self._vacate_sanctions(conn, actor, old_case, patch, effective)

    # ------------------------------------------------------------------
    # Sanction period construction
    # ------------------------------------------------------------------

    def _other_athlete_sanctions(self, conn, athlete_id, case_id):
        return [
            s for s in self.repository.list_entities(kind="sanction", conn=conn)
            if s["data"].get("athlete_id") == athlete_id
            and s["data"].get("case_id") != case_id
            and s["status"] in ("active", "pending_backfill")
        ]

    def _issue_active_sanction(self, conn, actor, old_case, merged_case, patch, effective):
        case_view = {
            "id": old_case["id"],
            "data": {
                "athlete_id": old_case["data"]["athlete_id"],
                "provisional_segments": merged_case.get("provisional_segments", []),
            },
        }
        other = self._other_athlete_sanctions(
            conn, old_case["data"]["athlete_id"], old_case["id"]
        )
        data = build_sanction_period(
            case_view, patch["decided_at"], patch["suspension_months"], other
        )
        data["version_no"] = self._next_version_no(effective)
        data["reason"] = patch.get("reason", "")
        annulled = self._annul_results_in_window(
            conn, actor, data["athlete_id"],
            data["ineligibility_start_date"], data["end_date"],
            old_case["id"],
        )
        data["annulled_results"] = annulled
        self._store_sanction_version(
            conn, actor, old_case, patch, effective, data, "active"
        )

    def _issue_pending_sanction(self, conn, actor, old_case, patch, effective):
        data = {
            "athlete_id": old_case["data"]["athlete_id"],
            "case_id": old_case["id"],
            "version_no": self._next_version_no(effective),
            "status_detail": "pending_backfill",
            "note": "禁赛决定缺少处罚期，待补录月数",
            "decided_at": patch["decided_at"],
            "reason": patch.get("reason", ""),
            "annulled_results": [],
        }
        self._store_sanction_version(
            conn, actor, old_case, patch, effective, data, "pending_backfill"
        )

    def _vacate_sanctions(self, conn, actor, old_case, patch, effective):
        for sanction in effective:
            self._supersede(
                conn, sanction, actor, "vacate",
                {"reason": patch.get("reason", ""), "decided_at": patch["decided_at"]},
                new_status="vacated",
            )
            self.repository.append_audit(
                sanction["id"], actor.user_id, actor.role, "vacate",
                sanction["status"], "vacated", {"case_id": old_case["id"]}, conn=conn,
            )

    @staticmethod
    def _next_version_no(effective):
        if not effective:
            return 1
        return max(int(s["data"].get("version_no", 1)) for s in effective) + 1

    def _store_sanction_version(
        self, conn, actor, old_case, patch, effective, data, status
    ):
        # Supersede prior effective versions of the same case; rows and
        # annulled results are retained.
        for sanction in effective:
            self._supersede(
                conn, sanction, actor,
                "redecide" if status == "active" else "redecide_pending",
                {"reason": patch.get("reason", ""), "decided_at": patch["decided_at"]},
            )
        sanction_id = "sanction-%s" % uuid4()
        now = utcnow()
        data = dict(data)
        data.setdefault("created_at", now)
        data["issued_at"] = now
        data["issued_by"] = actor.user_id
        entity = self.repository.create_entity(
            sanction_id, "sanction", status, data, actor.user_id, conn=conn
        )
        self.repository.append_audit(
            sanction_id, actor.user_id, actor.role, "sanction_issued",
            None, status,
            {"case_id": old_case["id"], "version_no": data.get("version_no")},
            conn=conn,
        )
        return entity

    def _supersede(self, conn, sanction, actor, action, detail, new_status="superseded"):
        data = dict(sanction["data"])
        history = data.setdefault("versions", [])
        history.append({
            "status_before": sanction["status"],
            "superseded_at": utcnow(),
            "superseded_by": actor.user_id,
            "action": action,
            "detail": detail,
        })
        self.repository.update_entity(
            sanction["id"], sanction["version"], new_status, data, conn=conn
        )
        self.repository.append_audit(
            sanction["id"], actor.user_id, actor.role, action,
            sanction["status"], new_status, detail, conn=conn,
        )

    # ------------------------------------------------------------------
    # Results annulled by a sanction window
    # ------------------------------------------------------------------

    def _annul_results_in_window(
        self, conn, actor, athlete_id, start_date, end_date, case_id
    ):
        annulled = []
        results = self.repository.find_entities(
            "result", "athlete_id", athlete_id, conn=conn
        )
        for result in results:
            event_date = result["data"].get("event_date", "")
            if result["status"] == "annulled":
                # Already void (possibly by a superseded version): keep the
                # original result and its existing annulment traces.
                annulled.append(result["id"])
                continue
            if start_date <= event_date <= end_date:
                data = dict(result["data"])
                trace = data.setdefault("annulments", [])
                trace.append({
                    "annulled_at": utcnow(),
                    "annulled_by": actor.user_id,
                    "window_start": start_date,
                    "window_end": end_date,
                    "case_id": case_id,
                })
                self.repository.update_entity(
                    result["id"], result["version"], "annulled", data, conn=conn
                )
                annulled.append(result["id"])
                self.repository.append_audit(
                    result["id"], actor.user_id, actor.role, "annul_result",
                    result["status"], "annulled",
                    {"window": [start_date, end_date], "case_id": case_id},
                    conn=conn,
                )
        return annulled

    # ------------------------------------------------------------------
    # Backfill a pending sanction period (same checks as a fresh decision)
    # ------------------------------------------------------------------

    def _activate_backfill(self, conn, actor, sanction_entity, patch):
        case = self.repository.get_entity(sanction_entity["data"]["case_id"], conn=conn)
        if not case:
            raise ValidationError("backfill references a missing case")
        other = self._other_athlete_sanctions(
            conn, sanction_entity["data"]["athlete_id"], case["id"]
        )
        data = build_sanction_period(
            case, patch["decided_at"], patch["suspension_months"], other
        )
        data["version_no"] = int(sanction_entity["data"].get("version_no", 1))
        data["backfilled_at"] = utcnow()
        data["backfilled_by"] = actor.user_id
        data["annulled_results"] = self._annul_results_in_window(
            conn, actor, data["athlete_id"],
            data["ineligibility_start_date"], data["end_date"],
            case["id"],
        )
        updated = self.repository.update_entity(
            sanction_entity["id"], sanction_entity["version"], "active", data, conn=conn
        )
        self.repository.append_audit(
            sanction_entity["id"], actor.user_id, actor.role, "backfill",
            "pending_backfill", "active",
            {"suspension_months": patch["suspension_months"]}, conn=conn,
        )
        return updated

    # ------------------------------------------------------------------
    # Legacy cases: decisions written before sanction periods existed
    # ------------------------------------------------------------------

    def provision_legacy_cases(self, actor=None, conn=None):
        """Each old closed case whose last decision is 'sanction' but has no
        sanction period row gets a pending_backfill placeholder. Idempotent."""
        actor_role = actor.role if actor else "admin"
        if conn is not None:
            return self._provision_legacy_cases(conn, actor_role)
        with self.repository.transaction() as tx:
            return self._provision_legacy_cases(tx, actor_role)

    def _provision_legacy_cases(self, conn, actor_role):
        cases = self.repository.list_entities(kind="case", conn=conn)
        sanctions = self.repository.list_entities(kind="sanction", conn=conn)
        covered = {s["data"].get("case_id") for s in sanctions}
        created = []
        for case in cases:
            if case["status"] != "closed" or case["id"] in covered:
                continue
            data = case["data"]
            # Old versions recorded the decision in different places:
            # structured history, a flat field, or free-text remarks.
            decisions = data.get("decisions", [])
            if decisions:
                last_decision = decisions[-1]
                is_sanction = last_decision.get("decision") == "sanction"
                decided_at = last_decision.get("decided_at")
            elif data.get("decision"):
                is_sanction = data.get("decision") == "sanction"
                decided_at = data.get("decided_at")
            else:
                note_text = " ".join(
                    str(data.get(key, ""))
                    for key in ("old_decision_note", "decision_note", "notes", "note", "remark")
                )
                negative = any(
                    token in note_text
                    for token in ("不予禁赛", "不构成禁赛", "无禁赛", "撤销禁赛", "no_sanction")
                )
                if negative or not note_text.strip() or (
                    "sanction" not in note_text and "禁赛" not in note_text
                ):
                    continue
                is_sanction = True
                decided_at = data.get("decided_at")
            if not is_sanction:
                continue
            decided_at = decided_at or date.today().isoformat()
            data = {
                "athlete_id": case["data"]["athlete_id"],
                "case_id": case["id"],
                "version_no": 1,
                "status_detail": "pending_backfill",
                "note": "老案升级后缺少处罚期，待补录",
                "decided_at": decided_at,
                "annulled_results": [],
                "legacy": True,
                "issued_at": utcnow(),
                "issued_by": SYSTEM_ACTOR_ID,
            }
            sanction_id = "sanction-%s" % uuid4()
            self.repository.create_entity(
                sanction_id, "sanction", "pending_backfill", data,
                SYSTEM_ACTOR_ID, conn=conn,
            )
            self.repository.append_audit(
                sanction_id, SYSTEM_ACTOR_ID, actor_role,
                "legacy_sanction_pending", None, "pending_backfill",
                {"case_id": case["id"]}, conn=conn,
            )
            created.append(sanction_id)
        return created

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def eligibility(self, athlete_id, as_of=None):
        as_of = as_of or date.today().isoformat()
        with self.repository.transaction() as conn:
            self.provision_legacy_cases(conn=conn)
            athlete = self.repository.get_entity(athlete_id, conn=conn)
            if not athlete:
                raise NotFoundError("athlete not found: " + athlete_id)
            sanctions = self.repository.list_entities(kind="sanction", conn=conn)
            tests = self.repository.list_entities(kind="comeback_test", conn=conn)
            cases = self.repository.list_entities(kind="case", conn=conn)
            results = self.repository.list_entities(kind="result", conn=conn)
            decision = evaluate_eligibility(
                athlete_id, as_of, sanctions, tests, cases
            )
            mine = [
                self._sanction_view(s)
                for s in sanctions
                if s["data"].get("athlete_id") == athlete_id
            ]
            mine.sort(key=lambda s: (s["version_no"], s["id"]))
            valid_tests = [
                t for t in tests
                if t["data"].get("athlete_id") == athlete_id
                and t["data"].get("test_date", "") <= as_of
            ]
            valid_tests.sort(key=lambda t: t["data"].get("test_date", ""))
            my_results = [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "event_name": r["data"].get("event_name"),
                    "event_date": r["data"].get("event_date"),
                    "result": r["data"].get("result"),
                    "annulments": r["data"].get("annulments", []),
                }
                for r in results
                if r["data"].get("athlete_id") == athlete_id
            ]
            my_results.sort(key=lambda r: r["event_date"])
        decision["athlete_id"] = athlete_id
        decision["sanction_periods"] = mine
        decision["comeback_tests"] = [
            {
                "id": t["id"],
                "test_date": t["data"].get("test_date"),
                "status": t["status"],
            }
            for t in valid_tests
        ]
        decision["results"] = my_results
        return decision

    @staticmethod
    def _sanction_view(sanction):
        data = sanction["data"]
        return {
            "id": sanction["id"],
            "status": sanction["status"],
            "version_no": data.get("version_no"),
            "case_id": data.get("case_id"),
            "start_date": data.get("start_date"),
            "ineligibility_start_date": data.get("ineligibility_start_date"),
            "end_date": data.get("end_date"),
            "raw_end_date": data.get("raw_end_date"),
            "suspension_months": data.get("suspension_months"),
            "credited_days": data.get("credited_days", 0),
            "credits": data.get("credits", []),
            "annulled_results": data.get("annulled_results", []),
            "legacy": data.get("legacy", False),
            "note": data.get("note"),
        }
