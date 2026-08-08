#!/usr/bin/env bash
# Actualizacion segura de una instalacion existente de AI Guardian.
#
# La instalacion usa `pip install /opt/ai/guardian` (no editable, ver
# install.sh), asi que un simple `git pull` NO actualiza el paquete ya
# instalado en el venv. Este script resuelve eso de forma explicita:
# actualiza el codigo, reinstala el paquete, corre los tests, y SOLO si
# todo pasa reinicia el servicio. Nunca usa `git reset --hard` ni descarta
# cambios locales en silencio.
set -Eeuo pipefail

GUARDIAN_DIR="/opt/ai/guardian"
VENV_DIR="${GUARDIAN_DIR}/.venv"
SERVICE_NAME="ai-guardian"
REMOTE="${AI_GUARDIAN_UPDATE_REMOTE:-origin}"
BRANCH="${AI_GUARDIAN_UPDATE_BRANCH:-main}"
EXPECTED_REPO_MARKER="HDanielRossi/gpu_watchdog"

log() { echo "[update] $*"; }
err() { echo "[update] ERROR: $*" >&2; }

trap 'err "actualizacion abortada (linea ${LINENO})"' ERR

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        err "este script debe ejecutarse como root (usa sudo)."
        exit 1
    fi
}

step_check_repo() {
    cd "${GUARDIAN_DIR}"
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        err "'${GUARDIAN_DIR}' no es un repositorio git."
        exit 1
    fi
    local origin_url
    origin_url="$(git remote get-url "${REMOTE}" 2>/dev/null || true)"
    if [[ "${origin_url}" != *"${EXPECTED_REPO_MARKER}"* ]]; then
        err "el remoto '${REMOTE}' (${origin_url:-sin remoto configurado}) no parece ser ${EXPECTED_REPO_MARKER}. Abortando por seguridad."
        exit 1
    fi
    log "repositorio verificado: ${origin_url}"
}

step_check_clean_tree() {
    if [[ -n "$(git status --porcelain)" ]]; then
        err "hay cambios locales sin confirmar en ${GUARDIAN_DIR}:"
        git status --short >&2
        err "confirma o descarta esos cambios antes de actualizar (este script NUNCA ejecuta 'git reset --hard' automaticamente)."
        exit 1
    fi
}

step_pull() {
    log "obteniendo cambios de ${REMOTE}/${BRANCH}..."
    git fetch "${REMOTE}" "${BRANCH}"
    local before after
    before="$(git rev-parse HEAD)"
    git merge --ff-only "${REMOTE}/${BRANCH}"
    after="$(git rev-parse HEAD)"
    if [[ "${before}" == "${after}" ]]; then
        log "ya estaba actualizado (${after:0:12}), no hay commits nuevos."
    else
        log "actualizado: ${before:0:12} -> ${after:0:12}"
    fi
}

step_reinstall_package() {
    if [[ ! -x "${VENV_DIR}/bin/pip" ]]; then
        err "no se encontro el entorno virtual en ${VENV_DIR}. Corre install.sh primero."
        exit 1
    fi
    log "reinstalando el paquete en el entorno virtual..."
    "${VENV_DIR}/bin/pip" install --quiet --upgrade "${GUARDIAN_DIR}"
}

step_run_tests() {
    log "ejecutando tests antes de reiniciar el servicio..."
    if ! "${VENV_DIR}/bin/pip" show pytest >/dev/null 2>&1; then
        "${VENV_DIR}/bin/pip" install --quiet "${GUARDIAN_DIR}[dev]"
    fi
    "${VENV_DIR}/bin/python" -m pytest -q
}

step_restart_service() {
    log "tests correctos: reiniciando ${SERVICE_NAME}..."
    systemctl restart "${SERVICE_NAME}.service"
}

step_show_status() {
    sleep 1
    systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
}

main() {
    require_root
    step_check_repo
    step_check_clean_tree
    step_pull
    step_reinstall_package

    if ! step_run_tests; then
        err "los tests fallaron: el servicio NO se reinicia. Revisa la salida de pytest arriba y corrige antes de reintentar."
        exit 1
    fi

    step_restart_service
    step_show_status
    log "actualizacion completa."
}

main "$@"
