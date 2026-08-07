#!/usr/bin/env bash
# Instalador de AI Guardian.
#
# No destructivo: nunca sobreescribe una configuracion existente sin
# respaldo explicito, y pide confirmacion antes de cualquier cambio de
# permisos (grupo docker) o de crear el usuario de servicio.
set -euo pipefail

GUARDIAN_DIR="/opt/ai/guardian"
VENV_DIR="${GUARDIAN_DIR}/.venv"
SERVICE_USER="ai-guardian"
SERVICE_GROUP="ai-guardian"
ETC_DIR="/etc/ai-guardian"
LOG_DIR="/var/log/ai-guardian"
CONFIG_PATH="${ETC_DIR}/config.yaml"
ENV_PATH="${ETC_DIR}/guardian.env"
SYSTEMD_UNIT_SRC="${GUARDIAN_DIR}/ai-guardian.service"
SYSTEMD_UNIT_DST="/etc/systemd/system/ai-guardian.service"

log() { echo "[install] $*"; }
err() { echo "[install] ERROR: $*" >&2; }

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        err "este script debe ejecutarse como root (usa sudo)."
        exit 1
    fi
}

confirm() {
    local prompt="$1"
    local reply
    read -r -p "${prompt} [y/N] " reply
    [[ "${reply}" =~ ^[Yy]$ ]]
}

step_check_linux() {
    if [[ "$(uname -s)" != "Linux" ]]; then
        err "AI Guardian solo esta soportado en Linux."
        exit 1
    fi
    log "sistema operativo: $(uname -s) $(uname -r)"
}

step_check_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 no encontrado en PATH."
        exit 1
    fi
    local version
    version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    local major minor
    major="${version%%.*}"
    minor="${version##*.}"
    if (( major < 3 || (major == 3 && minor < 11) )); then
        err "se requiere Python >= 3.11, se encontro ${version}."
        exit 1
    fi
    log "python3 detectado: ${version}"
}

step_check_nvidia() {
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        err "nvidia-smi no encontrado. AI Guardian puede instalarse igual (el monitor de GPU degradara a 'sin datos'), pero revisa el driver NVIDIA."
        if ! confirm "¿Continuar sin nvidia-smi disponible?"; then
            exit 1
        fi
    else
        log "nvidia-smi detectado: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
    fi
}

step_check_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        err "docker no encontrado. AI Guardian puede instalarse igual (el monitor de Docker degradara a 'no disponible'), pero revisa la instalacion de Docker."
        if ! confirm "¿Continuar sin Docker disponible?"; then
            exit 1
        fi
    else
        log "docker detectado: $(docker --version)"
    fi
}

step_create_venv() {
    if [[ -d "${VENV_DIR}" ]]; then
        log "entorno virtual ya existe en ${VENV_DIR}, se reutiliza."
    else
        log "creando entorno virtual en ${VENV_DIR}..."
        python3 -m venv "${VENV_DIR}"
    fi
    log "instalando dependencias..."
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet -r "${GUARDIAN_DIR}/requirements.txt"
    "${VENV_DIR}/bin/pip" install --quiet "${GUARDIAN_DIR}"
}

step_create_service_user() {
    if id "${SERVICE_USER}" >/dev/null 2>&1; then
        log "el usuario '${SERVICE_USER}' ya existe."
    else
        log "creando usuario de servicio '${SERVICE_USER}' (sin login, sin home)..."
        useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
    fi

    echo
    log "AVISO: el grupo 'docker' otorga control total sobre el daemon Docker,"
    log "lo cual equivale en la practica a privilegios de root en este servidor"
    log "(un proceso con acceso al socket de Docker puede montar cualquier ruta"
    log "del host dentro de un contenedor). AI Guardian solo necesita ese acceso"
    log "para leer el estado de los contenedores y, si tu lo habilitas mas"
    log "adelante, detener/reiniciar los que configures."
    if confirm "¿Agregar '${SERVICE_USER}' al grupo 'docker' ahora?"; then
        usermod -aG docker "${SERVICE_USER}"
        log "usuario agregado al grupo docker."
    else
        log "omitido. El monitor de Docker reportara 'no disponible' hasta que agregues el usuario al grupo docker manualmente:"
        log "  sudo usermod -aG docker ${SERVICE_USER}"
    fi
}

step_create_directories() {
    install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${LOG_DIR}"
    install -d -m 0750 -o root -g "${SERVICE_GROUP}" "${ETC_DIR}"
    log "directorios listos: ${LOG_DIR} ${ETC_DIR}"
}

step_install_config() {
    if [[ -f "${CONFIG_PATH}" ]]; then
        log "ya existe una configuracion en ${CONFIG_PATH}, no se sobreescribe."
    else
        install -m 0640 -o root -g "${SERVICE_GROUP}" "${GUARDIAN_DIR}/config.example.yaml" "${CONFIG_PATH}"
        log "configuracion de ejemplo copiada a ${CONFIG_PATH} (dry_run: true por defecto)."
    fi

    if [[ -f "${ENV_PATH}" ]]; then
        log "ya existe ${ENV_PATH}, no se sobreescribe."
    else
        cat > "${ENV_PATH}" <<'EOF'
# Variables de entorno para AI Guardian (tokens, secretos).
# Nunca se versiona ni se registra en logs. Ejemplo para Telegram:
# AI_GUARDIAN_TELEGRAM_TOKEN=
# AI_GUARDIAN_TELEGRAM_CHAT_ID=
EOF
        chmod 0640 "${ENV_PATH}"
        chown root:"${SERVICE_GROUP}" "${ENV_PATH}"
        log "plantilla de variables de entorno creada en ${ENV_PATH}."
    fi
}

step_install_systemd_unit() {
    if [[ -f "${SYSTEMD_UNIT_DST}" ]]; then
        local backup="${SYSTEMD_UNIT_DST}.bak.$(date +%Y%m%d%H%M%S)"
        log "ya existe una unidad systemd instalada; se respalda en ${backup}."
        cp "${SYSTEMD_UNIT_DST}" "${backup}"
    fi
    install -m 0644 "${SYSTEMD_UNIT_SRC}" "${SYSTEMD_UNIT_DST}"
    systemctl daemon-reload
    log "unidad systemd instalada y recargada."
}

step_validate_config() {
    log "validando configuracion..."
    if ! "${VENV_DIR}/bin/ai-guardian" --config "${CONFIG_PATH}" validate-config; then
        err "la configuracion no es valida. Corrige ${CONFIG_PATH} antes de habilitar el servicio."
        exit 1
    fi
}

step_enable_start() {
    if confirm "¿Habilitar e iniciar el servicio ai-guardian ahora?"; then
        systemctl enable --now ai-guardian.service
        log "servicio habilitado e iniciado."
    else
        log "omitido. Puedes hacerlo despues con: sudo systemctl enable --now ai-guardian"
    fi
}

main() {
    require_root
    step_check_linux
    step_check_python
    step_check_nvidia
    step_check_docker
    step_create_venv
    step_create_service_user
    step_create_directories
    step_install_config
    step_install_systemd_unit
    step_validate_config
    step_enable_start

    echo
    log "instalacion completa. Comandos utiles:"
    echo "  sudo systemctl status ai-guardian"
    echo "  sudo journalctl -u ai-guardian -f"
    echo "  sudo -u ${SERVICE_USER} ${VENV_DIR}/bin/ai-guardian --config ${CONFIG_PATH} check"
    echo "  sudo -u ${SERVICE_USER} ${VENV_DIR}/bin/ai-guardian --config ${CONFIG_PATH} metrics"
}

main "$@"
