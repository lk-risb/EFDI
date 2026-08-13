#!/usr/bin/env bash
# Self-heal the deployment, then run every repository test and static check.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT/compose/.env"
COMPOSE_FILE="$ROOT/compose/docker-compose.yml"
UI="$ROOT/compose/zenoh-admin/ui"
PYTHON="$ROOT/compose/venv/bin/python3"

# shellcheck source=scripts/_spinner.sh
. "$ROOT/scripts/_spinner.sh"
# shellcheck source=scripts/_selftest.sh
. "$ROOT/scripts/_selftest.sh"

[ -f "$ENV_FILE" ] || fail "compose/.env not found — run ./install.sh first"
[ -d "$ROOT/.git" ] || fail "Not a git repo — clone via git, not a manual download"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
cd "$ROOT"

banner "Health Check"

if ! command -v pnpm >/dev/null 2>&1 && ! command -v npx >/dev/null 2>&1; then
    info "Node.js/pnpm not found — installing (needed for the WebUI type-check)…"
    PKG_MGR=""
    if command -v apt-get >/dev/null 2>&1; then PKG_MGR="apt"
    elif command -v dnf >/dev/null 2>&1; then PKG_MGR="dnf"
    fi
    case "$PKG_MGR" in
        apt)
            curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - >/dev/null 2>&1
            sudo apt-get install -y -qq nodejs
            ;;
        dnf)
            curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash - >/dev/null 2>&1
            sudo dnf install -y -q nodejs
            ;;
        *)
            fail "No supported package manager (apt/dnf) found — install Node.js manually and re-run."
            ;;
    esac
    command -v node >/dev/null 2>&1 || fail "Node.js installation failed — install it manually and re-run."
    sudo npm install -g pnpm@11.9.0
fi

if command -v pnpm >/dev/null 2>&1; then
    PNPM=(pnpm)
else
    PNPM=(npx --yes pnpm@11.9.0)
fi

git_commit="$(git rev-parse HEAD)"
admin_image="$(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" images -q zenoh-admin 2>/dev/null)"
deployed_commit=""
if [ -n "$admin_image" ]; then
    deployed_commit="$(docker inspect --format \
        '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$admin_image" 2>/dev/null || true)"
fi
if [ "$deployed_commit" != "$git_commit" ]; then
    warn "Stale zenoh-admin image detected — rebuilding without cache"
    export GIT_COMMIT="$git_commit"
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build --no-cache zenoh-admin \
        || fail "Clean zenoh-admin rebuild failed"
    admin_image="$(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" images -q zenoh-admin)"
    deployed_commit="$(docker inspect --format \
        '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$admin_image" 2>/dev/null || true)"
    [ "$deployed_commit" = "$git_commit" ] \
        || fail "Clean rebuild still does not carry revision ${git_commit:0:7}"
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --remove-orphans \
        || fail "Infrastructure restart failed"
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" restart zenoh-admin-proxy \
        || fail "zenoh-admin-proxy restart failed"
    ok "Clean build now matches ${git_commit:0:7}"
else
    ok "Deployed zenoh-admin image matches ${git_commit:0:7}"
fi

info "Host discovery"
"$PYTHON" "$ROOT/scripts/detect-host.py"

info "Python compile checks"
mapfile -t python_files < <(
    find "$ROOT/compose/bridges" "$ROOT/compose/layers" "$ROOT/compose/protocols" \
         "$ROOT/compose/zenoh-admin/api" -type f -name '*.py' -print | sort
)
"$PYTHON" -m py_compile "${python_files[@]}"
ok "Python modules compile"

info "Python tests"
# pytest is test-only tooling, not a runtime pod dependency — it deliberately
# isn't in compose/requirements.txt (CI installs it ad hoc too, see
# .github/workflows/python-tests.yml). install.sh's venv never has it, so a
# fresh install's first health.sh run always needs to fetch it here.
if ! "$PYTHON" -m pytest --version &>/dev/null; then
    if ! "$PYTHON" -m pip --version &>/dev/null; then
        # A venv created before install.sh's ensurepip fix existed has
        # python3 but no pip at all — `pip install` can't bootstrap itself.
        # Recreate in place with the system interpreter (venv creation on an
        # existing directory fills in what's missing, it doesn't wipe it);
        # the system now has python3-venv/python3-pip either from install.sh
        # or from here, so this repairs pip without losing installed packages.
        info "pip missing from venv — repairing…"
        SYSTEM_PYTHON="$(command -v python3)"
        if ! "$SYSTEM_PYTHON" -m ensurepip --version &>/dev/null; then
            if command -v apt-get >/dev/null 2>&1; then
                sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv python3-pip
            elif command -v dnf >/dev/null 2>&1; then
                sudo dnf install -y -q python3-pip
            fi
        fi
        "$SYSTEM_PYTHON" -m venv "$ROOT/compose/venv"
        "$PYTHON" -m pip --version &>/dev/null \
            || fail "pip is still missing from the venv after repair — delete compose/venv and re-run install.sh"
    fi
    info "pytest not found in venv — installing (needed for the self-test)…"
    "$PYTHON" -m pip install --quiet --disable-pip-version-check pytest
fi
PYTHONPATH="$ROOT/compose/generated:$ROOT/compose/generated/protocols:$ROOT/compose" \
    "$PYTHON" -m pytest -q "$ROOT/tests"
ok "Python tests passed"

info "Shell syntax and ShellCheck"
mapfile -d '' -t shell_files < <(
    git ls-files -co --exclude-standard -z -- '*.sh'
)
for index in "${!shell_files[@]}"; do
    shell_files[index]="$ROOT/${shell_files[index]}"
    bash -n "${shell_files[index]}"
done
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck "${shell_files[@]}"
else
    warn "shellcheck is not installed; shell syntax passed but linting was skipped"
fi
ok "Shell checks passed"

info "Compose rendering"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" config -q
for overlay in docker-compose.child.yml docker-compose.grandchild.yml; do
    if [ -f "$ROOT/compose/$overlay" ]; then
        docker compose -f "$ROOT/compose/$overlay" --env-file "$ENV_FILE" config -q
    fi
done
ok "Compose files render"

info "Frontend type-check and build"
if [ ! -d "$UI/node_modules" ]; then
    (cd "$UI" && "${PNPM[@]}" install --frozen-lockfile)
fi
(cd "$UI" && "${PNPM[@]}" type-check && "${PNPM[@]}" build)
ok "Frontend checks passed"

info "Executable tests"
while IFS= read -r test_script; do
    "$test_script"
done < <(find "$ROOT/tests" -type f -name '*.sh' -perm -u+x -print | sort)
ok "Executable tests passed"

efdi_selftest || fail "Live EFDI self-test failed after self-heal"

info "Whitespace and optional secret scan"
git diff --check
if command -v gitleaks >/dev/null 2>&1; then
    gitleaks detect --source "$ROOT" --no-banner --redact
else
    warn "gitleaks is not installed; tracked-file secret scan was skipped"
fi

info "Container status"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps \
    --format 'table {{.Name}}\t{{.Status}}'

printf '\n'
printf '  %b┌────────────────────────────────────────────────┐%b\n' "$G" "$NC"
printf '  %b│%b  %bHealth check passed%b                          %b│%b\n' \
    "$G" "$NC" "$W" "$NC" "$G" "$NC"
printf '  %b└────────────────────────────────────────────────┘%b\n\n' "$G" "$NC"
