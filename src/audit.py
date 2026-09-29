from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditTrail:
    def __init__(self, repository):
        self.repository = repository

    def record(self, entity_id, actor, action, from_status, to_status,
               detail=None, store=None):
        payload = {
            "entity_id": entity_id,
            "actor_id": actor.user_id,
            "actor_role": actor.role,
            "action": action,
            "from_status": from_status,
            "to_status": to_status,
            "detail": detail or {},
        }
        if store is not None:
            store.append_audit(**payload)
        else:
            self.repository.append_audit(**payload)

    def list(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
