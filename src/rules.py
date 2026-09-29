import calendar
from datetime import date, datetime

from .domain import (
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

REENTRY_TESTS_REQUIRED = 3


# ---------------------------------------------------------------------------
# Date / interval helpers (pure domain math, no storage)
# ---------------------------------------------------------------------------

def as_date(value):
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        raise ValidationError("date is required")
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        raise ValidationError("invalid date: " + str(value))


def add_months(day, months):
    month0 = day.month - 1 + int(months)
    year = day.year + month0 // 12
    month = month0 % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def merge_intervals(intervals):
    """Merge inclusive ordinal date intervals."""
    merged = []
    for start, end in intervals:
        if start > end:
            continue
        merged.append([start, end])
    merged.sort()
    result = []
    for start, end in merged:
        if result and start <= result[-1][1] + 1:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return [(a, b) for a, b in result]


def clip_and_merge(intervals, cutoff_ordinal):
    """Merge intervals and cap their right side at the decision date."""
    capped = [(a, min(b, cutoff_ordinal)) for a, b in intervals if a <= cutoff_ordinal]
    return merge_intervals(capped)


def interval_days(intervals):
    return sum(end - start + 1 for start, end in intervals)


def subtract_intervals(base, blocked):
    """Remove any blocked days from base (inclusive ordinal intervals)."""
    blocked_set = set()
    for start, end in blocked:
        blocked_set.update(range(start, end + 1))
    out = []
    for start, end in base:
        chunk = None
        for ordinal in range(start, end + 1):
            if ordinal not in blocked_set:
                if chunk is None:
                    chunk = [ordinal, ordinal]
                else:
                    chunk[1] = ordinal
            elif chunk is not None:
                out.append(tuple(chunk))
                chunk = None
        if chunk is not None:
            out.append(tuple(chunk))
    return out


# ---------------------------------------------------------------------------
# Per-entity validators
# ---------------------------------------------------------------------------

def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    if data.get("decision") == "sanction":
        months = data.get("suspension_months")
        if months not in (None, ""):
            try:
                months = int(months)
            except (TypeError, ValueError):
                raise ValidationError("suspension_months must be a positive integer")
            if months <= 0:
                raise ValidationError("suspension_months must be a positive integer")
            data["suspension_months"] = months
    return {"decided_by": actor.user_id}


def _validate_result(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete:
        raise ValidationError("result requires an athlete")
    as_date(data.get("event_date"))
    if not str(data.get("event_name", "")).strip():
        raise ValidationError("event_name is required")
    if not str(data.get("result", "")).strip():
        raise ValidationError("result is required")


def _validate_comeback_test(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete:
        raise ValidationError("comeback_test requires an athlete")
    as_date(data.get("test_date"))


def _validate_sanction_backfill(actor, entity, data, lookup):
    if entity["status"] != "pending_backfill":
        raise InvalidTransition("only a pending_backfill sanction period can be backfilled")
    try:
        months = int(data.get("suspension_months"))
    except (TypeError, ValueError):
        raise ValidationError("suspension_months must be a positive integer")
    if months <= 0:
        raise ValidationError("suspension_months must be a positive integer")
    data["suspension_months"] = months
    if data.get("decided_at"):
        as_date(data.get("decided_at"))
    return {"backfilled_by": actor.user_id}


CUSTOM_CREATE = {
    'athlete': _validate_athlete,
    'sample': _validate_sample,
    'case': _validate_case,
    'result': _validate_result,
    'comeback_test': _validate_comeback_test,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'report_adverse'): _validate_report_adverse,
    ('case', 'decide'): _validate_case_decision,
    ('case', 'resolve_appeal'): _validate_case_decision,
    ('sanction', 'backfill'): _validate_sanction_backfill,
}


class RuleEngine:
    ALIASES = {
        'athletes': 'athlete',
        'samples': 'sample',
        'cases': 'case',
        'sanctions': 'sanction',
        'results': 'result',
        'comeback_tests': 'comeback_test',
    }
    INITIAL_STATUS = {
        'athlete': 'active',
        'sample': 'scheduled',
        'case': 'open',
        'sanction': 'active',
        'result': 'recorded',
        'comeback_test': 'recorded',
    }
    TRANSITIONS = {
        'athlete': {
            'retire': (('active',), 'retired'),
        },
        'sample': {
            'collect': (('scheduled',), 'collected'),
            'seal': (('collected',), 'sealed'),
            'ship': (('sealed',), 'in_transit'),
            'receive': (('in_transit',), 'received'),
            'analyze': (('received',), 'analyzed'),
            'report_adverse': (('analyzed',), 'adverse'),
            'clear': (('analyzed',), 'cleared'),
        },
        'case': {
            'provisional_suspend': (('open', 'suspended'), 'suspended'),
            'revoke_provisional': (('suspended',), 'open'),
            'schedule_hearing': (('suspended', 'open'), 'hearing'),
            'decide': (('hearing',), 'closed'),
            'appeal': (('closed', 'appeal'), 'appeal'),
            'resolve_appeal': (('appeal',), 'closed'),
        },
        'sanction': {
            'backfill': (('pending_backfill',), 'active'),
        },
    }
    CREATE_REQUIRED = {
        'athlete': ('name', 'discipline'),
        'sample': ('athlete_id', 'sample_code', 'event'),
        'case': ('athlete_id', 'sample_id', 'alleged_rule'),
        'result': ('athlete_id', 'event_name', 'event_date', 'result'),
        'comeback_test': ('athlete_id', 'test_date'),
    }
    ACTION_REQUIRED = {
        ('sample', 'collect'): ('collected_at',),
        ('sample', 'seal'): ('seal_id',),
        ('sample', 'ship'): ('carrier',),
        ('sample', 'receive'): ('lab_id',),
        ('sample', 'analyze'): ('result',),
        ('sample', 'clear'): ('reason',),
        ('case', 'provisional_suspend'): ('reason',),
        ('case', 'revoke_provisional'): ('reason',),
        ('case', 'schedule_hearing'): ('hearing_at',),
        ('case', 'decide'): ('decision',),
        ('case', 'appeal'): ('grounds',),
        ('case', 'resolve_appeal'): ('decision',),
        ('sanction', 'backfill'): ('suspension_months',),
    }
    CREATE_ROLES = {
        'athlete': ('admin', 'panel'),
        'sample': ('admin', 'inspector'),
        'case': ('admin', 'panel'),
        # sanction periods are generated by decisions, never created directly
        'sanction': (),
        'result': ('admin', 'inspector'),
        'comeback_test': ('admin', 'inspector', 'lab'),
    }
    ROLE_ACTIONS = {
        'retire': ('admin', 'panel'),
        'collect': ('admin', 'inspector'),
        'seal': ('admin', 'inspector'),
        'ship': ('admin', 'inspector'),
        'receive': ('admin', 'lab'),
        'analyze': ('admin', 'lab'),
        'report_adverse': ('admin', 'lab'),
        'clear': ('admin', 'lab'),
        'provisional_suspend': ('admin', 'panel'),
        'revoke_provisional': ('admin', 'panel'),
        'schedule_hearing': ('admin', 'panel'),
        'decide': ('admin', 'panel'),
        'appeal': ('admin', 'panel'),
        'resolve_appeal': ('admin', 'panel'),
        ('sanction', 'backfill'): ('admin', 'panel'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if not allowed:
            raise PermissionDenied("this kind is generated by the system")
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Provisional suspension segments
# ---------------------------------------------------------------------------

def provisional_segments(case_entity, closed_at=None):
    """Return inclusive ordinal intervals actually served (never revoked)
    for a single case. Segments are recorded on the case data by the
    service: each has start_date, and either end_date or revoked_date."""
    segments = case_entity["data"].get("provisional_segments", [])
    cutoff = as_date(closed_at).toordinal() if closed_at else None
    intervals = []
    for segment in segments:
        if segment.get("revoked_date"):
            # Revoked provisional suspension never counts as credit.
            continue
        start = as_date(segment["start_date"]).toordinal()
        end_text = segment.get("end_date")
        end = as_date(end_text).toordinal() if end_text else (cutoff or start)
        if cutoff is not None:
            end = min(end, cutoff)
        if start <= end:
            intervals.append((start, end))
    return clip_and_merge(intervals, cutoff) if cutoff is not None else merge_intervals(intervals)


# ---------------------------------------------------------------------------
# Sanction period computation
# ---------------------------------------------------------------------------

def _active_sanctions(sanctions):
    out = []
    for sanction in sanctions:
        if sanction["kind"] == "sanction" and sanction["status"] in ("active", "pending_backfill"):
            out.append(sanction)
    out.sort(key=lambda item: (item["data"].get("start_date", ""), item["id"]))
    return out


def compute_credit(case_entity, decision_date, other_sanctions):
    """Credit for consecutive, never-revoked provisional service in *this*
    case, minus days already credited by other sanction periods of the same
    athlete (overlapping cases cannot double-credit)."""
    served = provisional_segments(case_entity, closed_at=decision_date)
    claimed = []
    for other in other_sanctions:
        for item in other["data"].get("credits", []):
            for interval in item.get("segments", []):
                claimed.append((int(interval[0]), int(interval[1])))
    credited = subtract_intervals(served, claimed)
    return {
        "case_id": case_entity["id"],
        "served_days": interval_days(served),
        "credited_days": interval_days(credited),
        "overlap_days": interval_days(served) - interval_days(credited),
        "segments": credited,
    }


def build_sanction_period(case_entity, decision_date_text, months, other_sanctions):
    """Pure construction of a sanction period payload after a decision."""
    decision_date = as_date(decision_date_text)
    raw_end = add_months(decision_date, months)
    raw_days = (raw_end - decision_date).days + 1
    credit = compute_credit(case_entity, decision_date, other_sanctions)
    credited_days = min(credit["credited_days"], raw_days - 1)
    end_ordinal = raw_end.toordinal() - credited_days
    # Results are void from the start of the (never-revoked) provisional
    # suspension that preceded the decision, or the decision date itself.
    served = provisional_segments(case_entity, closed_at=decision_date)
    ineligibility_start = (
        date.fromordinal(served[0][0]).isoformat() if served else decision_date.isoformat()
    )
    return {
        "athlete_id": case_entity["data"]["athlete_id"],
        "case_id": case_entity["id"],
        "start_date": decision_date.isoformat(),
        "ineligibility_start_date": ineligibility_start,
        "raw_end_date": raw_end.isoformat(),
        "end_date": date.fromordinal(end_ordinal).isoformat(),
        "suspension_months": int(months),
        "raw_days": raw_days,
        # Keep the credit record even when overlap left zero creditable days.
        "credits": [credit] if credit["served_days"] else [],
        "credited_days": credited_days,
        "results_annulled": [],
        "versions": [],
    }


# ---------------------------------------------------------------------------
# Eligibility at a point in time
# ---------------------------------------------------------------------------

def evaluate_eligibility(athlete_id, as_of, sanctions, comeback_tests, cases=None):
    """Decide whether the athlete may be entered / have a result recorded
    for a date. Reads the sanction periods (and active provisional
    suspensions) effective at that date."""
    as_of_date = as_date(as_of)
    as_of_text = as_of_date.isoformat()

    # A provisional suspension is itself an ineligibility period, even
    # before a decision generates the formal sanction period.
    provisional_blocks = []
    for case in cases or []:
        if case["data"].get("athlete_id") != athlete_id:
            continue
        for segment in case["data"].get("provisional_segments", []):
            if segment.get("revoked_date"):
                continue
            if segment["start_date"] <= as_of_text and (
                not segment.get("end_date") or segment["end_date"] >= as_of_text
            ):
                provisional_blocks.append(case["id"])
                break
    if provisional_blocks:
        return {
            "eligible": False,
            "reason": "provisionally_suspended",
            "as_of": as_of_text,
            "blocking_case_ids": provisional_blocks,
        }

    active = [s for s in _active_sanctions(sanctions) if s["data"].get("athlete_id") == athlete_id]
    pending = [s for s in active if s["status"] == "pending_backfill"]
    effective = [s for s in active if s["status"] == "active"]

    if pending:
        return {
            "eligible": False,
            "reason": "pending_backfill",
            "as_of": as_of_date.isoformat(),
            "blocking_sanction_ids": [s["id"] for s in pending],
        }

    blocking = [
        s for s in effective
        if s["data"].get("ineligibility_start_date", s["data"].get("start_date", ""))
        <= as_of_text
        <= s["data"].get("end_date", "")
    ]
    if blocking:
        return {
            "eligible": False,
            "reason": "suspended",
            "as_of": as_of_date.isoformat(),
            "blocking_sanction_ids": [s["id"] for s in blocking],
        }

    # After the latest sanction period ends, three comeback tests are
    # required before eligibility is restored.
    expired = [s for s in effective if s["data"].get("end_date", "") < as_of_text]
    if expired:
        latest = max(expired, key=lambda s: s["data"]["end_date"])
        needed = latest["data"].get("end_date")
        valid_tests = [
            t for t in comeback_tests
            if t["data"].get("athlete_id") == athlete_id
            and t["data"].get("test_date", "") > needed
            and t["data"].get("test_date", "") <= as_of_text
        ]
        if len(valid_tests) < REENTRY_TESTS_REQUIRED:
            return {
                "eligible": False,
                "reason": "awaiting_comeback_tests",
                "as_of": as_of_date.isoformat(),
                "blocking_sanction_ids": [latest["id"]],
                "comeback_tests_required": REENTRY_TESTS_REQUIRED,
                "comeback_tests_completed": len(valid_tests),
            }

    return {"eligible": True, "reason": "eligible", "as_of": as_of_text}
