from calendar import monthrange
from datetime import date, datetime

from .domain import (
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


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


def _validate_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    if data["decision"] == "sanction":
        _require_positive_months(data.get("months"))
    else:
        data.pop("months", None)
    if data.get("decision_date"):
        parse_date(data["decision_date"])
    return {"decided_by": actor.user_id}


def _validate_provisional_suspend(actor, entity, data, lookup):
    started = parse_date(data.get("started_at"))
    if started > date.today():
        raise ValidationError("provisional suspension cannot start in the future")
    return {}


def _validate_competition_result(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete:
        raise ValidationError("competition_result requires an athlete")
    event_date = parse_date(data.get("event_date"))
    if event_date > date.today():
        raise ValidationError("cannot register a result for a future competition")
    if not str(data.get("event", "")).strip():
        raise ValidationError("event is required")
    if not str(data.get("placing", "")).strip():
        raise ValidationError("placing is required")
    data["annulled"] = False
    return {}


def _validate_comeback_test(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete:
        raise ValidationError("comeback_test requires an athlete")
    test_date = parse_date(data.get("test_date"))
    if test_date > date.today():
        raise ValidationError("cannot record a test in the future")
    if data.get("result") not in ("negative",):
        raise ValidationError("only a negative comeback test restores eligibility")
    return {}


def _require_positive_months(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError("months must be a positive integer")
    return value


CUSTOM_CREATE = {
    'athlete': _validate_athlete,
    'sample': _validate_sample,
    'case': _validate_case,
    'competition_result': _validate_competition_result,
    'comeback_test': _validate_comeback_test,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'report_adverse'): _validate_report_adverse,
    ('case', 'decide'): _validate_decision,
    ('case', 'resolve_appeal'): _validate_decision,
    ('case', 'provisional_suspend'): _validate_provisional_suspend,
}


class RuleEngine:
    ALIASES = {
        'athletes': 'athlete',
        'samples': 'sample',
        'cases': 'case',
        'sanctions': 'sanction',
        'provisional_suspensions': 'provisional_suspension',
        'competition_results': 'competition_result',
        'comeback_tests': 'comeback_test',
    }
    INITIAL_STATUS = {
        'athlete': 'active',
        'sample': 'scheduled',
        'case': 'open',
        'sanction': 'pending_backfill',
        'provisional_suspension': 'in_force',
        'competition_result': 'registered',
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
            'lift_provisional': (('suspended',), 'open'),
            'schedule_hearing': (('open', 'suspended'), 'hearing'),
            'decide': (('hearing',), 'closed'),
            'appeal': (('closed',), 'appeal'),
            'resolve_appeal': (('appeal',), 'closed'),
        },
        'sanction': {
            'backfill': (('pending_backfill',), 'active'),
            'supersede': (('active', 'pending_backfill'), 'superseded'),
        },
    }
    CREATE_REQUIRED = {
        'athlete': ('name', 'discipline'),
        'sample': ('athlete_id', 'sample_code', 'event'),
        'case': ('athlete_id', 'sample_id', 'alleged_rule'),
        'competition_result': ('athlete_id', 'event', 'event_date', 'placing'),
        'comeback_test': ('athlete_id', 'test_date', 'result'),
    }
    ACTION_REQUIRED = {
        ('sample', 'collect'): ('collected_at',),
        ('sample', 'seal'): ('seal_id',),
        ('sample', 'ship'): ('carrier',),
        ('sample', 'receive'): ('lab_id',),
        ('sample', 'analyze'): ('result',),
        ('sample', 'clear'): ('reason',),
        ('case', 'provisional_suspend'): ('reason', 'started_at'),
        ('case', 'lift_provisional'): ('reason',),
        ('case', 'schedule_hearing'): ('hearing_at',),
        ('case', 'decide'): ('decision',),
        ('case', 'appeal'): ('grounds',),
        ('case', 'resolve_appeal'): ('decision',),
        ('sanction', 'backfill'): ('months',),
    }
    CREATE_ROLES = {
        'athlete': ('admin', 'panel'),
        'sample': ('admin', 'inspector'),
        'case': ('admin', 'panel'),
        'competition_result': ('admin', 'inspector'),
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
        'lift_provisional': ('admin', 'panel'),
        'schedule_hearing': ('admin', 'panel'),
        'decide': ('admin', 'panel'),
        'appeal': ('admin', 'panel'),
        'resolve_appeal': ('admin', 'panel'),
        'backfill': ('admin', 'panel'),
        'supersede': ('admin', 'panel'),
    }
    # Kinds that only the use case layer may instantiate directly.
    SYSTEM_KINDS = frozenset({'sanction', 'provisional_suspension'})

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
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
# Date and range calculations (inclusive day ranges)
# ---------------------------------------------------------------------------


def parse_date(value):
    if value is None:
        raise ValidationError("a date value is required")
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        raise ValidationError("invalid date: %s" % value)


def add_months(day, months):
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    last_day = monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def merge_ranges(ranges):
    """Union of inclusive (start, end) date ranges, sorted."""
    ordered = sorted((s, e) for s, e in ranges if s <= e)
    merged = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [(s, e) for s, e in merged]


def range_days(ranges):
    return sum((end - start).days + 1 for start, end in merge_ranges(ranges))


def date_in_ranges(day, ranges):
    return any(start <= day <= end for start, end in ranges)


def _subtract_ranges(base, removed):
    removed = merge_ranges(removed)
    pieces = []
    for start, end in merge_ranges(base):
        current = [(start, end)]
        for rstart, rend in removed:
            surviving = []
            for pstart, pend in current:
                if rend < pstart or rstart > pend:
                    surviving.append((pstart, pend))
                else:
                    if rstart > pstart:
                        surviving.append(
                            (pstart, date.fromordinal(rstart.toordinal() - 1))
                        )
                    if rend < pend:
                        surviving.append(
                            (date.fromordinal(rend.toordinal() + 1), pend)
                        )
            current = surviving
        pieces.extend(current)
    return merge_ranges(pieces)


def _provisional_ranges(provisionals, cap=None, case_id=None, inclusive_cap=True):
    ranges = []
    for item in provisionals:
        if item["status"] != "in_force":
            continue
        matches_case = (
            case_id is None or item["data"].get("case_id") == case_id
        )
        if not matches_case:
            continue
        start = parse_date(item["data"]["started_at"])
        end_value = item["data"].get("ended_at")
        end = parse_date(end_value) if end_value else date.today()
        if cap is not None and end > cap:
            end = cap
        if not inclusive_cap and end == cap:
            end = date.fromordinal(cap.toordinal() - 1)
        if start <= end:
            ranges.append((start, end))
    return merge_ranges(ranges)


def build_sanction_period(case, months, decision_day, all_provisionals,
                          already_credited_ranges=()):
    """Build the sanction period for a case.

    Credit = un-revoked provisional suspension served on this case, merged
    into continuous runs. Days already credited by other cases are removed
    so overlapping cases never double-count.
    """
    raw_end = add_months(decision_day, months)
    # Credit covers days before the sanction itself starts; the decision day
    # belongs to the formal sanction window, not the provisional credit.
    case_ranges = _provisional_ranges(
        all_provisionals, cap=decision_day, case_id=case["id"],
        inclusive_cap=False,
    )
    credited_ranges = _subtract_ranges(case_ranges, already_credited_ranges)
    credited_days = range_days(credited_ranges)
    adjusted_end = date.fromordinal(raw_end.toordinal() - credited_days)
    if adjusted_end < decision_day:
        adjusted_end = decision_day
    blocked = merge_ranges(credited_ranges + [(decision_day, adjusted_end)])
    return {
        "months": int(months),
        "decision_date": decision_day.isoformat(),
        "raw_start": decision_day.isoformat(),
        "raw_end": raw_end.isoformat(),
        "credited_days": credited_days,
        "adjusted_end": adjusted_end.isoformat(),
        "credit_ranges": [
            {"start": s.isoformat(), "end": e.isoformat(),
             "days": (e - s).days + 1}
            for s, e in credited_ranges
        ],
        "blocked_ranges": [
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in blocked
        ],
    }


def evaluate_eligibility(athlete_id, on_date, sanctions, comeback_tests):
    """Decide eligibility for an athlete on a given day.

    Pending backfills always block (legacy case not upgraded yet). An active
    sanction blocks on every credited/blocked day. After the latest blocked
    day, three negative comeback tests are required.
    """
    pending = [s for s in sanctions if s["status"] == "pending_backfill"]
    active = [s for s in sanctions if s["status"] == "active"]

    if pending:
        return {
            "eligible": False,
            "reason": "pending_backfill",
            "detail": {
                "sanction_ids": [s["id"] for s in pending],
                "case_ids": [s["data"].get("case_id") for s in pending],
            },
        }

    blocked_today = []
    latest_end_ordinal = None
    for sanction in active:
        ranges = [
            (parse_date(r["start"]), parse_date(r["end"]))
            for r in sanction["data"].get("blocked_ranges", [])
        ]
        if date_in_ranges(on_date, ranges):
            blocked_today.append(sanction["id"])
        for _, end in ranges:
            if latest_end_ordinal is None or end.toordinal() > latest_end_ordinal:
                latest_end_ordinal = end.toordinal()

    if blocked_today:
        return {
            "eligible": False,
            "reason": "suspended",
            "detail": {"sanction_ids": blocked_today},
        }

    if latest_end_ordinal is not None:
        latest_end = date.fromordinal(latest_end_ordinal)
        if on_date <= latest_end:
            return {"eligible": True, "reason": "no_active_period", "detail": {}}
        valid_tests = [
            t for t in comeback_tests
            if parse_date(t["data"]["test_date"]) > latest_end
            and parse_date(t["data"]["test_date"]) <= on_date
            and t["status"] == "recorded"
            and t["data"].get("result") == "negative"
        ]
        if len(valid_tests) >= 3:
            test_dates = [
                parse_date(t["data"]["test_date"]).isoformat()
                for t in valid_tests[:3]
            ]
            return {
                "eligible": True,
                "reason": "reinstated",
                "detail": {
                    "tests": [t["id"] for t in valid_tests[:3]],
                    "since": max(test_dates),
                },
            }
        return {
            "eligible": False,
            "reason": "comeback_tests_incomplete",
            "detail": {
                "completed": len(valid_tests),
                "required": 3,
                "after": latest_end.isoformat(),
            },
        }

    return {"eligible": True, "reason": "no_sanction", "detail": {}}
