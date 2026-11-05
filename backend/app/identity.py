"""Request-scoped identity; background work receives an explicit owner."""
from contextlib import contextmanager
from contextvars import ContextVar

owner_context: ContextVar[str | None] = ContextVar('bloomate_owner', default=None)


def owner_id() -> str:
    owner = owner_context.get()
    if not owner:
        raise RuntimeError('Identity is required')
    return owner


@contextmanager
def acting_as(owner: str):
    token = owner_context.set(owner)
    try:
        yield
    finally:
        owner_context.reset(token)


def run_owned(owner: str, function, *args):
    from app.auth import connect, owner_active
    if not owner_active(owner):
        # Expiry must also finish queued work, otherwise cleanup could wait forever.
        tables = {'process':'jobs', 'process_adjustment':'plan_adjustments',
                  'process_habit_plan_revision':'habit_plan_revisions',
                  'process_growth_suggestion':'growth_suggestions', 'process_habit_advice':'habit_advice'}
        table = tables.get(function.__name__)
        if table and args:
            with connect() as db:
                db.execute(f"UPDATE {table} SET status='error',error_code='EXPIRED' WHERE id=? AND owner_id=? AND status IN ('pending','running')", (args[0], owner))
        return
    with acting_as(owner):
        function(*args)
