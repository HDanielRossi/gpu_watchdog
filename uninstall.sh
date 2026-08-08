#!/usr/bin/env bash
# Desinstalador de AI Guardian.
#
# Por defecto solo detiene y deshabilita el servicio. Cualquier eliminacion
# de datos (config, logs, entorno virtual, usuario de servicio) requiere
# confirmacion explicita, uno por uno.
set -euo pipefail

GUARDIAN_DIR="/opt/ai/guardian"
VENV_DIR="${GUARDIAN_DIR}/.venv"
SERVICE_USER="ai-guardian"
ETC_DIR="/etc/ai-guardian"
LOG_DIR="/var/log/ai-guardian"
SYSTEMD_UNIT_DST="/etc/systemd/system/ai-guardian.service"
POLKIT_RULE_DST="/etc/polkit-1/rules.d/49-ai-guardian-shutdown.rules"

log() { echo "[uninstall] $*"; }

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        echo "[uninstall] ERROR: este script debe ejecutarse como root (usa sudo)." >&2
        exit 1
    fi
}

confirm() {
    local prompt="$1"
    local reply
    read -r -p "${prompt} [y/N] " reply
    [[ "${reply}" =~ ^[Yy]$ ]]
}

main() {
    require_root

    if systemctl is-active --quiet ai-guardian.service 2>/dev/null; then
        log "deteniendo el servicio..."
        systemctl stop ai-guardian.service
    fi
    if systemctl is-enabled --quiet ai-guardian.service 2>/dev/null; then
        log "deshabilitando el servicio..."
        systemctl disable ai-guardian.service
    fi

    if [[ -f "${SYSTEMD_UNIT_DST}" ]]; then
        rm -f "${SYSTEMD_UNIT_DST}"
        systemctl daemon-reload
        log "unidad systemd eliminada."
    fi

    echo
    log "El servicio ya esta detenido y deshabilitado. Lo siguiente es opcional y destructivo:"

    if [[ -d "${VENV_DIR}" ]] && confirm "¿Eliminar el entorno virtual (${VENV_DIR})?"; then
        rm -rf "${VENV_DIR}"
        log "entorno virtual eliminado."
    fi

    if [[ -d "${ETC_DIR}" ]] && confirm "¿Eliminar la configuracion (${ETC_DIR}, incluye secretos en guardian.env)?"; then
        rm -rf "${ETC_DIR}"
        log "configuracion eliminada."
    fi

    if [[ -d "${LOG_DIR}" ]] && confirm "¿Eliminar los logs y el estado guardado (${LOG_DIR})?"; then
        rm -rf "${LOG_DIR}"
        log "logs eliminados."
    fi

    if id "${SERVICE_USER}" >/dev/null 2>&1 && confirm "¿Eliminar el usuario de servicio '${SERVICE_USER}'?"; then
        userdel "${SERVICE_USER}"
        log "usuario '${SERVICE_USER}' eliminado."
    fi

    if [[ -f "${POLKIT_RULE_DST}" ]] && confirm "¿Eliminar la regla polkit de apagado (${POLKIT_RULE_DST})?"; then
        rm -f "${POLKIT_RULE_DST}"
        systemctl restart polkit || true
        log "regla polkit eliminada."
    fi

    echo
    log "Desinstalacion terminada. El codigo fuente en ${GUARDIAN_DIR} no se toco; borralo manualmente si ya no lo necesitas."
}

main "$@"
