"""Internal service-to-service API (Phase 1: auth; Phase 2: read tools;
Phase 3: context snapshot, memory outbox, compliance review; Phase 5: write
enable + recovery authority; Phase 6: audit ingest + trace propagation).

Mounted under /internal by api.main. These endpoints are NOT the public API:
they authenticate the caller with an Internal Service JWT (a separate secret
and audience from the public user token) and are expected to be reachable only
on the internal network. Public auth semantics are deliberately not reused.
"""

from fastapi import APIRouter, Depends

from internal_api.audit import internal_trace, router as audit_router
from internal_api.auth import router as auth_router
from internal_api.compliance import router as compliance_router
from internal_api.context import router as context_router
from internal_api.memory import router as memory_router
from internal_api.operation_status import router as operation_status_router
from internal_api.tools import router as tools_router

internal_router = APIRouter()
# Phase 6: one dependency on every internal route attaches the request to the
# propagated trace. It is non-throwing by construction (see internal_api.audit)
# and adds nothing but a response header and an in-process record.
_TRACE = [Depends(internal_trace)]
internal_router.include_router(auth_router, dependencies=_TRACE)
internal_router.include_router(tools_router, dependencies=_TRACE)
internal_router.include_router(context_router, dependencies=_TRACE)
internal_router.include_router(memory_router, dependencies=_TRACE)
internal_router.include_router(compliance_router, dependencies=_TRACE)
internal_router.include_router(operation_status_router, dependencies=_TRACE)
internal_router.include_router(audit_router, dependencies=_TRACE)

__all__ = ["internal_router"]
