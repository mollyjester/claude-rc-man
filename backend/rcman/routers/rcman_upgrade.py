"""rcman itself, for administrators: the upgrade of rcman from its web interface.

The report says which release runs, whether the upgrade from the web interface is on, whether
one may be asked for now and why not, and how the last one went; a request is admitted or
refused by rcman_upgrade.py, which the upgrade's own units then carry out as root. What the upgrade
did is in the journal of ``rcman-upgrade.service`` and in its status; the audit trail records
the request only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from rcman import rcman_upgrade as upgrade
from rcman.audit import Record, Recorder
from rcman.identity import admin_user
from rcman.problems import Problem
from rcman.ratelimit import TokenBuckets

router = APIRouter(prefix="/api/rcman", tags=["rcman"], dependencies=[Depends(admin_user)])


def new_limit() -> TokenBuckets:
    """Each administrator's requests for an upgrade: REQUESTS_BURST at once, then
    REQUESTS_PER_MINUTE."""
    return TokenBuckets(upgrade.REQUESTS_BURST, upgrade.REQUESTS_PER_MINUTE / 60)


@router.get("/upgrade")
def report(request: Request) -> dict:
    """The release rcman runs (``release``, its commit, None in a development run), whether the
    upgrade from the web interface is on (``enabled``), whether one may be asked for now
    (``available``) and, where not, the problem a request would be refused with (``reason``),
    and the last upgrade: ``state`` (requested, running, succeeded, failed, not_started, or
    unknown where there is no status rcman can read) and its status's ``started``,
    ``finished``, ``from``, ``to`` and ``error``."""
    return request.app.state.upgrade.report()


@router.post("/upgrade", status_code=202)
def ask(request: Request, record: Record = Depends(Recorder("rcman.upgrade"))) -> dict:
    """Ask for rcman to be upgraded to its clone's ``main``: admitted, it is carried out in the
    background, rcman restarting on the way; refused with ``rcman_upgrade_refused`` and why. An
    administrator who asks more than REQUESTS_BURST times at once, or REQUESTS_PER_MINUTE a
    minute, is refused with 429 and the seconds to wait. The report as it is then."""
    limit = request.app.state.upgrade_limit
    if not limit.take(record.identity.email):
        wait = limit.seconds_until_allowed(record.identity.email)
        raise Problem("too_many_requests", headers={"Retry-After": str(wait)})
    return request.app.state.upgrade.request()
