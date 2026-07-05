# Partner Certificate Enrollment Portal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-service, web-based certificate enrollment flow to the zenoh-admin GUI, so a superadmin can invite a partner and the partner gets a signed cert (from the team's existing step-ca root CA on node-1) via one link, no terminal use.

**Architecture:** New `invites` Postgres table + `api/enrollment.py` FastAPI router added to the existing zenoh-admin backend; two new React routes (`/admin/enrollment` superadmin-only, `/enroll/$token` public). Certificate signing shells out to the `step` CLI against a dedicated JWK provisioner on node-1's step-ca.

**Tech Stack:** FastAPI, SQLAlchemy async, `cryptography` (keypair/CSR generation), `step` CLI (subprocess, cert signing), React + TanStack Router (existing zenoh-admin frontend stack).

## Global Constraints

- Private key is generated in memory only — never written to disk or the database, per the approved design (`docs/superpowers/specs/2026-07-05-partner-cert-enrollment-portal-design.md`).
- Invite tokens are single-use and time-limited; only the SHA-256 hash is stored (same pattern as `RefreshToken.token_hash` in `compose/zenoh-admin/api/models.py:34`).
- NetBird mesh enrollment is explicitly out of scope — handled separately via NetBird's own dashboard.
- No new test framework — this codebase verifies Python changes via `py_compile` + actual module execution (not pytest); follow that same pattern for every backend task.
- Never run `git commit` — stage and describe changes only; the user commits manually.
- After every edit to a Python file: `python3 -m py_compile <file>` (syntax) AND actually import/execute the module (catches module-level NameErrors that `py_compile` misses).

---

### Task 1: `Invite` database model

**Files:**
- Modify: `compose/zenoh-admin/api/models.py`

**Interfaces:**
- Consumes: `Base` from `compose/zenoh-admin/api/db.py`, `_uuid()` helper (models.py:9-10).
- Produces: `Invite` class with fields `id: str`, `token_hash: str`, `partner_name: str`, `namespace: str`, `created_by: str`, `created_at: datetime`, `expires_at: datetime`, `used_at: datetime | None` — consumed by Task 2/3's `enrollment.py`.

- [ ] **Step 1: Add the `Invite` model**

Append to `compose/zenoh-admin/api/models.py`:

```python
class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    partner_name: Mapped[str] = mapped_column(String(128), nullable=False)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("admin_users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
```

- [ ] **Step 2: Compile-check and execute**

```bash
cd /home/ndukve/IdeaProjects/efdi-moon-pod
python3 -m py_compile compose/zenoh-admin/api/models.py
```
Expected: no output (clean).

Then actually import it (module-level execution check, not just syntax):
```bash
ZENOH_ADMIN_DB_USER=test ZENOH_ADMIN_DB_PASSWORD=test ZENOH_ADMIN_SECRET_KEY=test-secret \
/tmp/zenoh-admin-test-venv/bin/python3 -c "
import sys; sys.path.insert(0, 'compose/zenoh-admin')
from api import models
print('Invite columns:', [c.name for c in models.Invite.__table__.columns])
"
```
Expected: `Invite columns: ['id', 'token_hash', 'partner_name', 'namespace', 'created_by', 'created_at', 'expires_at', 'used_at']`

If `/tmp/zenoh-admin-test-venv` doesn't exist (a prior session's throwaway venv for local verification since the host only has Python 3.14 and `asyncpg` needs 3.11-3.13), create one:
```bash
python3.11 -m venv /tmp/zenoh-admin-test-venv || python3 -m venv /tmp/zenoh-admin-test-venv
/tmp/zenoh-admin-test-venv/bin/pip install -r compose/zenoh-admin/requirements.txt
```

---

### Task 2: step-ca signing helper module

**Files:**
- Create: `compose/zenoh-admin/api/stepca.py`

**Interfaces:**
- Consumes: env vars `STEP_CA_URL`, `STEP_CA_ROOT_PATH`, `STEP_CA_PROVISIONER`, `STEP_CA_PROVISIONER_PASSWORD_FILE` (all new, wired in Task 7).
- Produces: `generate_keypair_and_csr(common_name: str) -> tuple[bytes, bytes]` (returns `(key_pem, csr_pem)`) and `sign_csr(csr_pem: bytes) -> bytes` (returns signed `cert_pem`, raises `StepCAError` on failure) — both consumed by Task 3's `enrollment.py`.

- [ ] **Step 1: Write the module**

```python
import os
import subprocess
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

STEP_CA_URL = os.environ.get("STEP_CA_URL", "")
STEP_CA_ROOT_PATH = os.environ.get("STEP_CA_ROOT_PATH", "")
STEP_CA_PROVISIONER = os.environ.get("STEP_CA_PROVISIONER", "")
STEP_CA_PROVISIONER_PASSWORD_FILE = os.environ.get("STEP_CA_PROVISIONER_PASSWORD_FILE", "")


class StepCAError(Exception):
    pass


def generate_keypair_and_csr(common_name: str) -> tuple[bytes, bytes]:
    """Generate an EC keypair + CSR in memory. Returns (key_pem, csr_pem)."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    return key_pem, csr_pem


def sign_csr(csr_pem: bytes) -> bytes:
    """Sign a CSR against node-1's step-ca via the `step` CLI. Returns cert_pem."""
    if not all([STEP_CA_URL, STEP_CA_ROOT_PATH, STEP_CA_PROVISIONER, STEP_CA_PROVISIONER_PASSWORD_FILE]):
        raise StepCAError("step-ca is not configured (STEP_CA_* env vars missing)")

    with tempfile.TemporaryDirectory() as tmp:
        csr_path = os.path.join(tmp, "request.csr")
        cert_path = os.path.join(tmp, "signed.crt")
        with open(csr_path, "wb") as f:
            f.write(csr_pem)

        result = subprocess.run(
            [
                "step", "ca", "sign", csr_path, cert_path,
                "--ca-url", STEP_CA_URL,
                "--root", STEP_CA_ROOT_PATH,
                "--provisioner", STEP_CA_PROVISIONER,
                "--provisioner-password-file", STEP_CA_PROVISIONER_PASSWORD_FILE,
                "--force",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise StepCAError("step ca sign failed: {}".format(result.stderr.strip()))

        with open(cert_path, "rb") as f:
            return f.read()
```

- [ ] **Step 2: Compile-check and execute**

```bash
cd /home/ndukve/IdeaProjects/efdi-moon-pod
python3 -m py_compile compose/zenoh-admin/api/stepca.py
```
Expected: no output.

```bash
/tmp/zenoh-admin-test-venv/bin/pip install cryptography --quiet
/tmp/zenoh-admin-test-venv/bin/python3 -c "
import sys; sys.path.insert(0, 'compose/zenoh-admin')
from api import stepca
key_pem, csr_pem = stepca.generate_keypair_and_csr('release/test-partner')
print('key starts with:', key_pem[:27])
print('csr starts with:', csr_pem[:32])
try:
    stepca.sign_csr(csr_pem)
except stepca.StepCAError as e:
    print('expected error (no STEP_CA_* env set):', e)
"
```
Expected: prints the PEM headers, then `expected error (no STEP_CA_* env set): step-ca is not configured (STEP_CA_* env vars missing)`.

---

### Task 3: Enrollment API router

**Files:**
- Create: `compose/zenoh-admin/api/enrollment.py`
- Modify: `compose/zenoh-admin/api/main.py`

**Interfaces:**
- Consumes: `Invite` model (Task 1), `stepca.generate_keypair_and_csr`/`stepca.sign_csr`/`stepca.StepCAError` (Task 2), `require_role`/`write_audit` from `compose/zenoh-admin/api/deps.py`, `get_db` from `compose/zenoh-admin/api/db.py`.
- Produces: `router` (FastAPI `APIRouter`) — consumed by `main.py`'s `app.include_router(...)`.

- [ ] **Step 1: Write the router**

```python
import hashlib
import io
import secrets
import zipfile
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .db import get_db
from .models import Invite
from .deps import require_role, write_audit
from . import stepca

router = APIRouter(tags=["enrollment"])

# Rendered into the downloaded config.json5 bundle — same shape as the fields
# compose/zenoh-admin/api/config.py already exposes, just without the local
# mTLS/TCP ports (the partner's own router picks its own).
_CONFIG_TEMPLATE = """\
{{
  mode: "router",
  plugins: {{
    storage_manager: {{
      storages: {{
        efdi_live: {{
          key_expr: "LTU/CISB/{namespace}/**",
          volume: {{ id: "memory" }},
        }},
      }},
    }},
  }},
}}
"""


class CreateInviteRequest(BaseModel):
    partner_name: str
    namespace: str
    expires_in_hours: int = 48


@router.post("/api/enrollment/invites", status_code=201)
async def create_invite(
    body: CreateInviteRequest,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=body.expires_in_hours)

    invite = Invite(
        token_hash=token_hash,
        partner_name=body.partner_name,
        namespace=body.namespace,
        created_by=actor.id,
        expires_at=expires_at,
    )
    db.add(invite)
    await db.commit()
    await write_audit(db, actor.id, "create_invite", body.partner_name)

    return {"token": raw_token, "expires_at": expires_at.isoformat()}


@router.get("/api/enrollment/invites")
async def list_invites(db: AsyncSession = Depends(get_db), _=Depends(require_role("superadmin"))):
    result = await db.execute(select(Invite).order_by(Invite.created_at.desc()))
    invites = result.scalars().all()
    now = datetime.now(timezone.utc)

    def _status(inv: Invite) -> str:
        if inv.used_at:
            return "used"
        if inv.expires_at.replace(tzinfo=timezone.utc) < now:
            return "expired"
        return "pending"

    return {"invites": [
        {
            "id": inv.id, "partner_name": inv.partner_name, "namespace": inv.namespace,
            "created_at": inv.created_at, "expires_at": inv.expires_at, "status": _status(inv),
        }
        for inv in invites
    ]}


async def _get_valid_invite(token: str, db: AsyncSession) -> Invite:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await db.execute(select(Invite).where(Invite.token_hash == token_hash))
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=410, detail="Invite not found")
    if invite.used_at:
        raise HTTPException(status_code=410, detail="Invite already used")
    if invite.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Invite expired")
    return invite


@router.get("/api/enroll/{token}")
async def check_invite(token: str, db: AsyncSession = Depends(get_db)):
    invite = await _get_valid_invite(token, db)
    return {"partner_name": invite.partner_name}


@router.post("/api/enroll/{token}")
async def enroll(token: str, db: AsyncSession = Depends(get_db)):
    invite = await _get_valid_invite(token, db)

    key_pem, csr_pem = stepca.generate_keypair_and_csr(invite.namespace)
    try:
        cert_pem = stepca.sign_csr(csr_pem)
    except stepca.StepCAError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    invite.used_at = datetime.now(timezone.utc)
    await db.commit()
    await write_audit(db, invite.created_by, "enroll_partner", invite.partner_name)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cert.pem", cert_pem)
        zf.writestr("key.pem", key_pem)
        zf.writestr("config.json5", _CONFIG_TEMPLATE.format(namespace=invite.namespace))
    buf.seek(0)

    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=enrollment-bundle.zip"},
    )
```

- [ ] **Step 2: Wire the router into `main.py`**

Modify `compose/zenoh-admin/api/main.py` — add the import next to the other router imports:

```python
from .enrollment import router as enrollment_router
```

And register it next to the other `app.include_router(...)` calls:

```python
app.include_router(enrollment_router)
```

- [ ] **Step 3: Compile-check and execute both files**

```bash
cd /home/ndukve/IdeaProjects/efdi-moon-pod
python3 -m py_compile compose/zenoh-admin/api/enrollment.py compose/zenoh-admin/api/main.py
```
Expected: no output.

```bash
ZENOH_ADMIN_DB_USER=test ZENOH_ADMIN_DB_PASSWORD=test ZENOH_ADMIN_SECRET_KEY=test-secret \
/tmp/zenoh-admin-test-venv/bin/python3 -c "
import sys; sys.path.insert(0, 'compose/zenoh-admin')
from api import main
print('main.py imports clean with enrollment router wired in')
"
```
Expected: `main.py imports clean with enrollment router wired in`.

- [ ] **Step 4: Functional test against a running container**

This requires the full stack up (see Task 7 for compose wiring). Once `zenoh-admin` is rebuilt and running:

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8890/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<your-superadmin-password>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s -X POST http://127.0.0.1:8890/api/enrollment/invites -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"partner_name":"Test Partner","namespace":"release/test-partner","expires_in_hours":1}'
```
Expected: `{"token":"<random-string>","expires_at":"..."}` with HTTP 201.

```bash
curl -s http://127.0.0.1:8890/api/enroll/<token-from-above>
```
Expected: `{"partner_name":"Test Partner"}` with HTTP 200 (no auth header needed — public endpoint).

If `STEP_CA_*` env vars aren't configured yet, `POST /api/enroll/<token>` returns `502` with the step-ca error message — that's expected until Task 7's compose wiring points at a real provisioner.

---

### Task 4: `step` CLI in the zenoh-admin image + compose wiring

**Files:**
- Modify: `compose/zenoh-admin/Dockerfile`
- Modify: `compose/docker-compose.yml`
- Modify: `compose/.env.example`

**Interfaces:**
- Consumes: nothing new.
- Produces: `step` binary on `$PATH` inside the `zenoh-admin` container; `STEP_CA_URL`, `STEP_CA_ROOT_PATH`, `STEP_CA_PROVISIONER`, `STEP_CA_PROVISIONER_PASSWORD_FILE` env vars available to `stepca.py` (Task 2) at runtime.

- [ ] **Step 1: Install `step` CLI in the Dockerfile**

Modify `compose/zenoh-admin/Dockerfile` — add after the `FROM python:3.11-slim` stage's `WORKDIR /app` line:

```dockerfile
# step CLI — used by api/stepca.py to sign partner CSRs against node-1's step-ca.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://github.com/smallstep/cli/releases/latest/download/step-cli_linux_amd64.tar.gz \
       -o /tmp/step-cli.tar.gz \
    && tar -xzf /tmp/step-cli.tar.gz -C /tmp \
    && mv /tmp/step-cli_*/bin/step /usr/local/bin/step \
    && rm -rf /tmp/step-cli.tar.gz /tmp/step-cli_* \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Add env vars + a secret mount to `docker-compose.yml`**

Modify the `zenoh-admin` service block in `compose/docker-compose.yml` — add to its `environment:` list:

```yaml
      - STEP_CA_URL=${STEP_CA_URL:-}
      - STEP_CA_ROOT_PATH=/step-ca/root_ca.crt
      - STEP_CA_PROVISIONER=${STEP_CA_PROVISIONER:-}
      - STEP_CA_PROVISIONER_PASSWORD_FILE=/step-ca/provisioner-password
```

And to its `volumes:` list:

```yaml
      - ${STEP_CA_ROOT_CERT_PATH:?set STEP_CA_ROOT_CERT_PATH in .env}:/step-ca/root_ca.crt:ro
      - ${STEP_CA_PROVISIONER_PASSWORD_PATH:?set STEP_CA_PROVISIONER_PASSWORD_PATH in .env}:/step-ca/provisioner-password:ro
```

- [ ] **Step 3: Document the new env vars in `.env.example`**

Append to `compose/.env.example`:

```bash
# ── Partner enrollment (step-ca on node-1) ──────────────────────────────────
STEP_CA_URL=https://node-1.example:9000
STEP_CA_PROVISIONER=zenoh-admin-enrollment
STEP_CA_ROOT_CERT_PATH=/path/to/step-ca/root_ca.crt
STEP_CA_PROVISIONER_PASSWORD_PATH=/path/to/step-ca/provisioner-password
```

- [ ] **Step 4: Verify the Dockerfile builds and `step` is on PATH**

```bash
cd /home/ndukve/IdeaProjects/efdi-moon-pod/compose
docker compose build zenoh-admin
docker compose run --rm --entrypoint sh zenoh-admin -c "step version"
```
Expected: prints a `Smallstep CLI/x.y.z` version line, confirming the binary is installed and executable.

---

### Task 5: Frontend — admin enrollment page

**Files:**
- Create: `compose/zenoh-admin/ui/src/routes/admin-enrollment.tsx`
- Modify: `compose/zenoh-admin/ui/src/components/Layout.tsx`

**Interfaces:**
- Consumes: `apiJson`/`apiFetch` from `@/lib/api`, `useAuth` from `@/store/auth`, `Layout` from `@/components/Layout` — same pattern as `admin-users.tsx`.
- Produces: `/admin-enrollment` route, reachable from the sidebar for `superadmin` role only.

- [ ] **Step 1: Write the page**

```tsx
import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { apiJson } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { toast } from 'sonner'
import { UserPlus } from 'lucide-react'

export const Route = createFileRoute('/admin-enrollment')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: EnrollmentPage,
})

interface Invite {
  id: string
  partner_name: string
  namespace: string
  created_at: string
  expires_at: string
  status: 'pending' | 'used' | 'expired'
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'text-green-400 bg-green-400/10',
  used: 'text-zinc-400 bg-zinc-400/10',
  expired: 'text-red-400 bg-red-400/10',
}

function NewInviteModal({ onClose, onCreated }: { onClose: () => void; onCreated: (link: string) => void }) {
  const [partnerName, setPartnerName] = useState('')
  const [namespace, setNamespace] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    try {
      const data = await apiJson<{ token: string }>('/api/enrollment/invites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ partner_name: partnerName, namespace, expires_in_hours: 48 }),
      })
      onCreated(`${window.location.origin}/enroll/${data.token}`)
      onClose()
    } catch (e: any) {
      toast.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 w-full max-w-sm">
        <h2 className="text-lg font-semibold mb-4">New Invite</h2>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="space-y-1">
            <label className="text-sm text-zinc-300">Partner name</label>
            <input type="text" value={partnerName} onChange={e => setPartnerName(e.target.value)} required
              className="w-full px-3 py-2 rounded-md bg-zinc-800 border border-zinc-700 text-white text-sm focus:outline-none focus:ring-2 focus:ring-accent-ring" />
          </div>
          <div className="space-y-1">
            <label className="text-sm text-zinc-300">Namespace</label>
            <input type="text" value={namespace} onChange={e => setNamespace(e.target.value)} required
              className="w-full px-3 py-2 rounded-md bg-zinc-800 border border-zinc-700 text-white text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-ring" />
          </div>
          <div className="flex gap-2 pt-1">
            <button type="button" onClick={onClose} className="flex-1 py-2 rounded bg-zinc-700 hover:bg-zinc-600 text-sm">Cancel</button>
            <button type="submit" disabled={loading}
              className="flex-1 py-2 rounded bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm disabled:opacity-50">
              {loading ? 'Creating…' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function EnrollmentPage() {
  const [invites, setInvites] = useState<Invite[]>([])
  const [showNew, setShowNew] = useState(false)

  async function load() {
    try {
      const data = await apiJson<{ invites: Invite[] }>('/api/enrollment/invites')
      setInvites(data.invites)
    } catch (e: any) { toast.error(e.message) }
  }

  useEffect(() => { load() }, [])

  function handleCreated(link: string) {
    navigator.clipboard.writeText(link)
    toast.success('Invite link copied to clipboard')
    load()
  }

  return (
    <Layout>
      <div className="p-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-xl font-semibold">Partner Enrollment</h1>
          <button onClick={() => setShowNew(true)}
            className="flex items-center gap-2 px-4 py-2 bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm rounded-md transition-colors">
            <UserPlus size={14} /> New Invite
          </button>
        </div>
        <div className="rounded-lg border border-zinc-800 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900 text-zinc-400">
              <tr>
                <th className="px-4 py-3 text-left font-medium">Partner</th>
                <th className="px-4 py-3 text-left font-medium">Namespace</th>
                <th className="px-4 py-3 text-left font-medium">Status</th>
                <th className="px-4 py-3 text-left font-medium">Expires</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800">
              {invites.length === 0 && (
                <tr><td colSpan={4} className="px-4 py-8 text-center text-zinc-500">No invites yet</td></tr>
              )}
              {invites.map(inv => (
                <tr key={inv.id} className="bg-zinc-950 hover:bg-zinc-900/50">
                  <td className="px-4 py-3">{inv.partner_name}</td>
                  <td className="px-4 py-3 font-mono text-xs">{inv.namespace}</td>
                  <td className="px-4 py-3">
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_COLORS[inv.status]}`}>{inv.status}</span>
                  </td>
                  <td className="px-4 py-3 text-zinc-500 text-xs">{new Date(inv.expires_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {showNew && <NewInviteModal onClose={() => setShowNew(false)} onCreated={handleCreated} />}
    </Layout>
  )
}
```

- [ ] **Step 2: Add the sidebar entry**

Modify `compose/zenoh-admin/ui/src/components/Layout.tsx` — add the icon import (`UserPlus` next to the existing `lucide-react` imports) and add to `superAdminItems`:

```tsx
const superAdminItems = [
  { to: '/admin-users', label: 'Admin Users', icon: ShieldUser },
  { to: '/admin-enrollment', label: 'Enrollment', icon: UserPlus },
]
```

- [ ] **Step 3: Type-check and build**

```bash
cd /home/ndukve/IdeaProjects/efdi-moon-pod/compose/zenoh-admin/ui
pnpm type-check
pnpm build
```
Expected: both exit clean, `dist/` regenerated.

---

### Task 6: Frontend — public enroll page

**Files:**
- Create: `compose/zenoh-admin/ui/src/routes/enroll.$token.tsx`

**Interfaces:**
- Consumes: plain `fetch` (this route is intentionally unauthenticated — no `apiFetch`/token-refresh machinery, since the partner has no login at all).
- Produces: `/enroll/$token` public route.

- [ ] **Step 1: Write the page**

```tsx
import { createFileRoute } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { brand } from '@/brand'

export const Route = createFileRoute('/enroll/$token')({
  component: EnrollPage,
})

function EnrollPage() {
  const { token } = Route.useParams()
  const [partnerName, setPartnerName] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(false)

  useEffect(() => {
    fetch(`/api/enroll/${token}`)
      .then(async res => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: res.statusText }))
          throw new Error(body.detail ?? res.statusText)
        }
        return res.json()
      })
      .then(data => setPartnerName(data.partner_name))
      .catch(e => setError(e.message))
  }, [token])

  async function handleEnroll() {
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`/api/enroll/${token}`, { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(body.detail ?? res.statusText)
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'enrollment-bundle.zip'
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      setDone(true)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-950">
      <div className="w-full max-w-sm space-y-6 p-8 rounded-xl border border-zinc-800 bg-zinc-900 text-center">
        <h1 className="text-2xl font-bold text-white">{brand.orgName}</h1>
        {error && <p className="text-red-400 text-sm">{error}</p>}
        {!error && partnerName && !done && (
          <>
            <p className="text-zinc-300">Welcome, <span className="font-semibold">{partnerName}</span>.</p>
            <button onClick={handleEnroll} disabled={loading}
              className="w-full py-2 rounded-md bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm font-medium disabled:opacity-50 transition-colors">
              {loading ? 'Enrolling…' : 'Enroll'}
            </button>
          </>
        )}
        {done && <p className="text-green-400 text-sm">Enrollment complete — bundle downloaded.</p>}
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Type-check and build**

```bash
cd /home/ndukve/IdeaProjects/efdi-moon-pod/compose/zenoh-admin/ui
pnpm type-check
pnpm build
```
Expected: both exit clean. Check `src/routeTree.gen.ts` now lists `/enroll/$token` alongside the existing routes.

- [ ] **Step 3: Full manual end-to-end test**

With the stack rebuilt and running (Task 4 done, `STEP_CA_*` pointed at a real provisioner):

1. Log into the GUI as superadmin, go to Enrollment, create an invite — confirm the link copies to clipboard.
2. Open the link in a private/incognito window (simulating the partner, no session).
3. Confirm the partner name renders, click Enroll.
4. Confirm a `enrollment-bundle.zip` downloads containing `cert.pem`, `key.pem`, `config.json5`.
5. Verify the cert against the CA root: `openssl verify -CAfile <step-ca-root.crt> cert.pem` → expected: `cert.pem: OK`.
6. Reload the same invite link — confirm it now shows the "already used" error (`410`).

---

### Task 7: Docs

**Files:**
- Modify: `INSTALL.md`
- Modify: `DIEGIMAS.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed elsewhere — pure documentation.

- [ ] **Step 1: Add an "Partner Enrollment" subsection to `INSTALL.md`**

Add after the existing "Isolated test router" subsection (in the Zenoh Admin GUI section):

```markdown
### Partner enrollment

Superadmins can invite a new partner from the GUI (Enrollment page): pick a partner name + namespace, share the generated link out-of-band. The partner opens the link and clicks Enroll — no terminal use — and downloads a bundle (`cert.pem`, `key.pem`, `config.json5`) signed by node-1's step-ca. NetBird mesh enrollment is handled separately, via NetBird's own dashboard.

Requires `STEP_CA_URL`, `STEP_CA_PROVISIONER`, `STEP_CA_ROOT_CERT_PATH`, `STEP_CA_PROVISIONER_PASSWORD_PATH` set in `compose/.env` (see `compose/.env.example`) — a dedicated JWK provisioner on node-1's step-ca, scoped to issuing these enrollment certs only.
```

- [ ] **Step 2: Add the Lithuanian equivalent to `DIEGIMAS.md`**

Add in the same relative position:

```markdown
### Partnerių registracija

Superadmin gali pakviesti naują partnerį iš GUI (Enrollment puslapis): parenka partnerio vardą + namespace, dalinasi sugeneruota nuoroda ne per šią sistemą. Partneris atidaro nuorodą ir paspaudžia Enroll — jokio terminalo naudojimo — ir atsisiunčia paketą (`cert.pem`, `key.pem`, `config.json5`), pasirašytą node-1 step-ca. NetBird tinklo registracija tvarkoma atskirai, per pačios NetBird valdymo skydelį.

Reikalauja `STEP_CA_URL`, `STEP_CA_PROVISIONER`, `STEP_CA_ROOT_CERT_PATH`, `STEP_CA_PROVISIONER_PASSWORD_PATH`, nustatytų `compose/.env` (žr. `compose/.env.example`) — dedikuotas JWK provisioner node-1 step-ca, skirtas tik šių registracijos sertifikatų išdavimui.
```

---

## Self-Review Notes

- **Spec coverage**: invite creation/listing (Task 3), enrollment action + bundle download (Task 3/6), step-ca signing (Task 2), server-side-only private key (Task 2/3 — key never written to disk), single-use/expiring tokens (Task 1/3), audit logging (Task 3, reuses `write_audit`), NetBird explicitly out of scope (not built anywhere in this plan) — all covered.
- **Type consistency**: `Invite` model fields (Task 1) match exactly what `enrollment.py` (Task 3) reads/writes. `stepca.generate_keypair_and_csr`/`sign_csr`/`StepCAError` signatures (Task 2) match their usage in Task 3.
- **Open item carried from the spec**: the exact step-ca provisioner name/password-file path on node-1 must be confirmed and node-1 configured with a matching JWK provisioner before Task 4/Task 3-Step-4 can succeed end-to-end — everything up to that point (Tasks 1-3, 5-6) is independently testable without a real step-ca connection.
