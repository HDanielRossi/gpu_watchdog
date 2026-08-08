#!/usr/bin/env bash
# Pruebas de shell para los chequeos "puros" de update.sh: repo invalido,
# remoto/repo equivocado, rama incorrecta, working tree sucio.
#
# Deliberadamente NO ejecuta la ruta de actualizacion real (fetch/worktree/
# venv temporal/pytest/merge/restart): eso requeriria red y, para la
# promocion final, root + systemd reales. Esas rutas felices ya se ejercen
# indirectamente via CI (install + pytest) y se documentan en el README;
# aqui se cubren especificamente las rutas de error que un revisor pidio
# verificar (rama incorrecta, tree sucio, repo/remoto equivocado).
#
# No requiere sudo/root: como el "dueno" del repo de prueba es el mismo
# usuario que ejecuta este script, update.sh corre las operaciones git de
# forma directa (ver as_user() en update.sh), sin invocar sudo -u.
#
# Uso: bash tests/test_update_sh.sh
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

pass_count=0
fail_count=0
out_file="$(mktemp)"
trap 'rm -f "${out_file}"' EXIT

assert_fails() {
    local description="$1"; shift
    if "$@" >"${out_file}" 2>&1; then
        echo "FAIL (se esperaba que fallara): ${description}"
        sed 's/^/    /' "${out_file}"
        fail_count=$((fail_count + 1))
    else
        echo "ok (fallo como se esperaba): ${description}"
        pass_count=$((pass_count + 1))
    fi
}

assert_succeeds() {
    local description="$1"; shift
    if "$@" >"${out_file}" 2>&1; then
        echo "ok: ${description}"
        pass_count=$((pass_count + 1))
    else
        echo "FAIL (se esperaba que pasara): ${description}"
        sed 's/^/    /' "${out_file}"
        fail_count=$((fail_count + 1))
    fi
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"; rm -f "${out_file}"' EXIT

# --- fixture: un remoto "bare" local y un clon de trabajo, ambos ficticios ---

REMOTE_DIR="${WORKDIR}/remote-gpu_watchdog.git"
git init --quiet --bare --initial-branch=main "${REMOTE_DIR}"

REPO_DIR="${WORKDIR}/guardian"
mkdir -p "${REPO_DIR}"
git -C "${REPO_DIR}" init --quiet --initial-branch=main
git -C "${REPO_DIR}" config user.email test@example.com
git -C "${REPO_DIR}" config user.name "Test"
git -C "${REPO_DIR}" remote add origin "${REMOTE_DIR}"
echo "contenido" > "${REPO_DIR}/archivo.txt"
git -C "${REPO_DIR}" add archivo.txt
git -C "${REPO_DIR}" commit --quiet -m "inicial"
git -C "${REPO_DIR}" push --quiet origin main

OWNER="$(id -un)"
export AI_GUARDIAN_DIR="${REPO_DIR}"
export AI_GUARDIAN_REPO_OWNER="${OWNER}"
export AI_GUARDIAN_REPO_MARKER="gpu_watchdog"

# shellcheck source=/dev/null
source "${REPO_ROOT}/update.sh"

echo "=== detect_repo_owner ==="
detected_owner="$(detect_repo_owner)"
if [[ "${detected_owner}" == "${OWNER}" ]]; then
    echo "ok: detect_repo_owner -> '${OWNER}'"
    pass_count=$((pass_count + 1))
else
    echo "FAIL: detect_repo_owner devolvio '${detected_owner}', esperado '${OWNER}'"
    fail_count=$((fail_count + 1))
fi

echo "=== check_is_git_repo ==="
GUARDIAN_DIR="${REPO_DIR}"
assert_succeeds "directorio con .git valido" check_is_git_repo "${OWNER}"

NON_REPO_DIR="${WORKDIR}/no-es-un-repo"
mkdir -p "${NON_REPO_DIR}"
GUARDIAN_DIR="${NON_REPO_DIR}"
assert_fails "directorio sin .git" check_is_git_repo "${OWNER}"
GUARDIAN_DIR="${REPO_DIR}"

echo "=== check_repo_marker ==="
EXPECTED_REPO_MARKER="gpu_watchdog"
assert_succeeds "remoto coincide con el marcador esperado" check_repo_marker "${OWNER}"

EXPECTED_REPO_MARKER="otra-organizacion/otro-repo"
assert_fails "remoto NO coincide con el marcador esperado (abortar por seguridad)" check_repo_marker "${OWNER}"
EXPECTED_REPO_MARKER="gpu_watchdog"

echo "=== check_correct_branch ==="
BRANCH="main"
assert_succeeds "rama actual (main) coincide con BRANCH" check_correct_branch "${OWNER}"

BRANCH="una-rama-que-no-es-esta"
assert_fails "rama actual NO coincide con BRANCH (abortar, no cambiar de rama solo)" check_correct_branch "${OWNER}"
BRANCH="main"

echo "=== check_clean_tree ==="
assert_succeeds "working tree limpio" check_clean_tree "${OWNER}"

echo "cambio local sin commitear" >> "${REPO_DIR}/archivo.txt"
assert_fails "working tree con cambios locales sin commitear (nunca reset --hard)" check_clean_tree "${OWNER}"
git -C "${REPO_DIR}" checkout --quiet -- archivo.txt

echo
echo "resultado: ${pass_count} ok, ${fail_count} fallidas"
if [[ "${fail_count}" -gt 0 ]]; then
    exit 1
fi
