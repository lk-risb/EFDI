"""WebUI-driven provisioning for a second, independent NetBird client
instance on this pod's host — needed because the pod's own NetBird network
(efdi.ltu) and the EFDI Backbone trial fabric are two separate NetBird
accounts, and a single netbird daemon can only join one account at a time
(confirmed live: --disable-profiles is set on this host's default instance).
See scripts/netbird-instance.sh for the actual provisioning logic; this
module only proxies the (management_url, setup_key) pair to the host control
agent, which is the one process with root to install a systemd unit.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .control import _control
from .deps import require_role

router = APIRouter(prefix="/api/netbird", tags=["netbird"])


class NetbirdInstanceRequest(BaseModel):
    management_url: str
    setup_key: str


@router.post("/{name}/configure")
async def configure_netbird_instance(
    name: str,
    body: NetbirdInstanceRequest,
    _=Depends(require_role("superadmin")),
):
    # The control agent restricts `name` to its own allowlist
    # (admin_control.py's _NETBIRD_INSTANCES) — no allowlist duplicated here,
    # a name outside it simply comes back as a 409 from the agent.
    return _control(
        f"/v1/netbird/{name}/configure",
        method="POST",
        body={"management_url": body.management_url, "setup_key": body.setup_key},
    )
