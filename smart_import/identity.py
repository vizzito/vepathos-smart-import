"""Identity comes from a verified transport credential, never from tool arguments."""
from contextvars import ContextVar

tenant: ContextVar[str | None] = ContextVar("smart_import_tenant", default=None)


def owns(job):
    principal = tenant.get()
    return principal is None or job.tenant_id == principal
