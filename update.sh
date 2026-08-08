#!/usr/bin/env bash
# Actualizacion transaccional y segura de una instalacion existente de
# AI Guardian.
#
# Garantia central: un candidato que falla los tests NUNCA sobreescribe el
# .venv real ni el working tree real. La secuencia es:
#
#   fetch candidato -> verificar candidato -> tests contra el candidato
#   (worktree + venv temporales, aislados) -> solo si pasan: promover
#   (merge --ff-only) + reinstalar el venv real -> restart -> verificar
#   estado.
#
# Si cualquier paso falla antes de "promover", el working tree real y el
# venv real quedan exactamente como estaban: la version previamente
# instalada sigue siendo la unica que systemd puede arrancar. Nunca se usa
# `git reset --hard` ni se descartan cambios locales.
#
# Separacion de privilegios: este script requiere root (necesita reinstalar
# en el venv real, que install.sh crea root-owned, y reiniciar el servicio
# systemd). Pero TODAS las operaciones de git (fetch, verificar rama/estado,
# worktree, merge) corren como el usuario dueño del repositorio via
# `sudo -u`, nunca como root directamente: evita que git deje archivos del
# working tree con ownership de root (rompiendo que el desarrollador humano
# pueda seguir usando el repo con normalidad) y usa las credenciales/
# SSH-agent/config git de ese usuario en vez de las de root, que
# normalmente no tiene ninguna configurada.
set -Eeuo pipefail

GUARDIAN_DIR="${AI_GUARDIAN_DIR:-/opt/ai/guardian}"
VENV_DIR="${GUARDIAN_DIR}/.venv"
SERVICE_NAME="ai-guardian"
REMOTE="${AI_GUARDIAN_UPDATE_REMOTE:-origin}"
BRANCH="${AI_GUARDIAN_UPDATE_BRANCH:-main}"
EXPECTED_REPO_MARKER="${AI_GUARDIAN_REPO_MARKER:-HDanielRossi/gpu_watchdog}"
REPO_OWNER_OVERRIDE="${AI_GUARDIAN_REPO_OWNER:-}"

log() { echo "[update] $*" >&2; }
err() { echo "[update] ERROR: $*" >&2; }

# ---------------------------------------------------------------------
# Utilidades de privilegio: separar "operaciones git" (como el dueno del
# repo) de "operaciones que de verdad requieren root".
# ---------------------------------------------------------------------

detect_repo_owner() {
    if [[ -n "${REPO_OWNER_OVERRIDE}" ]]; then
        echo "${REPO_OWNER_OVERRIDE}"
        return 0
    fi
    if [[ ! -e "${GUARDIAN_DIR}/.git" ]]; then
        echo "$(id -un)"
        return 0
    fi
    stat -c '%U' "${GUARDIAN_DIR}/.git" 2>/dev/null || id -un
}

# Ejecuta un comando como el usuario dado; si ya somos ese usuario, corre
# directo (sin sudo -u, que fallaria si no hay password-less sudo entre
# usuarios no-root, y es innecesario en ese caso).
as_user() {
    local target_user="$1"; shift
    if [[ "$(id -un)" == "${target_user}" ]]; then
        "$@"
    else
        sudo -u "${target_user}" -- "$@"
    fi
}

git_as_owner() {
    local owner="$1"; shift
    as_user "${owner}" git -C "${GUARDIAN_DIR}" "$@"
}

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        err "este script debe ejecutarse como root (usa sudo): necesita reinstalar en el venv real y reiniciar el servicio systemd."
        exit 1
    fi
}

# ---------------------------------------------------------------------
# Chequeos puros: sin red, sin root, sin systemd. Se pueden invocar
# directamente en pruebas de shell (ver tests/test_update_sh.sh).
# ---------------------------------------------------------------------

check_is_git_repo() {
    local owner="$1"
    if ! git_as_owner "${owner}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        err "'${GUARDIAN_DIR}' no es un repositorio git."
        return 1
    fi
}

check_repo_marker() {
    local owner="$1"
    local url
    url="$(git_as_owner "${owner}" remote get-url "${REMOTE}" 2>/dev/null || true)"
    if [[ "${url}" != *"${EXPECTED_REPO_MARKER}"* ]]; then
        err "el remoto '${REMOTE}' (${url:-sin remoto configurado}) no parece ser ${EXPECTED_REPO_MARKER}. Abortando por seguridad."
        return 1
    fi
    log "repositorio verificado: ${url}"
}

check_correct_branch() {
    local owner="$1"
    local current
    current="$(git_as_owner "${owner}" rev-parse --abbrev-ref HEAD)"
    if [[ "${current}" != "${BRANCH}" ]]; then
        err "la rama actual es '${current}', pero se espera '${BRANCH}' (ajustable con AI_GUARDIAN_UPDATE_BRANCH). Cambia de rama manualmente antes de actualizar; este script no lo hace por ti."
        return 1
    fi
    log "rama verificada: ${current}"
}

check_clean_tree() {
    local owner="$1"
    local dirty
    dirty="$(git_as_owner "${owner}" status --porcelain)"
    if [[ -n "${dirty}" ]]; then
        err "hay cambios locales sin confirmar en ${GUARDIAN_DIR}:"
        echo "${dirty}" >&2
        err "confirma o descarta esos cambios antes de actualizar (este script NUNCA ejecuta 'git reset --hard' automaticamente)."
        return 1
    fi
}

# ---------------------------------------------------------------------
# Candidato aislado: fetch + worktree temporal + venv temporal + tests.
# Nada de esto toca el working tree real ni el venv real.
# ---------------------------------------------------------------------

fetch_candidate_sha() {
    local owner="$1"
    log "obteniendo el candidato de ${REMOTE}/${BRANCH} (fetch, sin tocar el working tree real)..."
    git_as_owner "${owner}" fetch "${REMOTE}" "${BRANCH}" >&2
    git_as_owner "${owner}" rev-parse "${REMOTE}/${BRANCH}"
}

# Devuelve 0 si el candidato paso los tests, 2 si ya estamos actualizados
# (nada que hacer), 1 si el candidato fallo (o cualquier paso previo).
verify_candidate_in_isolation() {
    local owner="$1" candidate_sha="$2" worktree_dir="$3" candidate_venv="$4"

    local current_sha
    current_sha="$(git_as_owner "${owner}" rev-parse HEAD)"
    if [[ "${current_sha}" == "${candidate_sha}" ]]; then
        log "ya estas en el commit mas reciente (${current_sha:0:12}), nada que actualizar."
        return 2
    fi
    log "candidato: ${current_sha:0:12} -> ${candidate_sha:0:12}"

    log "creando worktree aislado para validar el candidato sin tocar ${GUARDIAN_DIR}..."
    git_as_owner "${owner}" worktree add --detach "${worktree_dir}" "${candidate_sha}"

    log "instalando el candidato en un venv temporal (el .venv real no se toca)..."
    as_user "${owner}" python3 -m venv "${candidate_venv}"
    as_user "${owner}" "${candidate_venv}/bin/pip" install --quiet --upgrade pip
    as_user "${owner}" "${candidate_venv}/bin/pip" install --quiet "${worktree_dir}[dev]"

    log "ejecutando tests contra el candidato..."
    as_user "${owner}" bash -c "cd '${worktree_dir}' && '${candidate_venv}/bin/python' -m pytest -q"
}

cleanup_candidate() {
    local owner="$1" worktree_dir="$2" candidate_venv="$3"
    rm -rf "${candidate_venv}" 2>/dev/null || true
    if [[ -d "${worktree_dir}" ]]; then
        git_as_owner "${owner}" worktree remove --force "${worktree_dir}" 2>/dev/null || rm -rf "${worktree_dir}"
    fi
    git_as_owner "${owner}" worktree prune >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------
# Promocion: solo se llega aqui si el candidato ya paso los tests en
# aislamiento. A partir de aqui SI se toca el working tree y el venv real.
# ---------------------------------------------------------------------

promote_candidate() {
    local owner="$1"
    log "candidato validado: actualizando el working tree real (fast-forward, sin reset --hard)..."
    git_as_owner "${owner}" merge --ff-only "${REMOTE}/${BRANCH}"
}

reinstall_real_venv() {
    if [[ ! -x "${VENV_DIR}/bin/pip" ]]; then
        err "no se encontro el entorno virtual en ${VENV_DIR}. Corre install.sh primero."
        return 1
    fi
    log "reinstalando el paquete ya validado en el venv real..."
    "${VENV_DIR}/bin/pip" install --quiet --upgrade "${GUARDIAN_DIR}"
}

restart_service() {
    log "reiniciando ${SERVICE_NAME}..."
    systemctl restart "${SERVICE_NAME}.service"
}

show_status() {
    sleep 1
    systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
}

main() {
    require_root

    local owner
    owner="$(detect_repo_owner)"
    log "operaciones de git corren como '${owner}' (nunca como root)."

    check_is_git_repo "${owner}"
    check_repo_marker "${owner}"
    check_correct_branch "${owner}"
    check_clean_tree "${owner}"

    local worktree_dir candidate_venv
    worktree_dir="$(as_user "${owner}" mktemp -d /tmp/ai-guardian-update-worktree.XXXXXX)"
    as_user "${owner}" rmdir "${worktree_dir}"  # git worktree add exige que el path NO exista aun
    candidate_venv="$(as_user "${owner}" mktemp -d /tmp/ai-guardian-update-venv.XXXXXX)"
    # shellcheck disable=SC2064
    trap "cleanup_candidate '${owner}' '${worktree_dir}' '${candidate_venv}'" EXIT

    local candidate_sha
    candidate_sha="$(fetch_candidate_sha "${owner}")"

    local verify_status=0
    verify_candidate_in_isolation "${owner}" "${candidate_sha}" "${worktree_dir}" "${candidate_venv}" || verify_status=$?

    if [[ "${verify_status}" -eq 2 ]]; then
        log "sin cambios que instalar."
        exit 0
    elif [[ "${verify_status}" -ne 0 ]]; then
        err "el candidato NO paso la validacion: la version instalada actualmente NO se toca y el servicio NO se reinicia."
        exit 1
    fi

    promote_candidate "${owner}"
    reinstall_real_venv
    restart_service
    show_status
    log "actualizacion completa."
}

# Permite que este archivo se "source"ee (p.ej. desde tests de shell) sin
# que main() se ejecute automaticamente.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
