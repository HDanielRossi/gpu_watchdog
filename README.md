# AI Guardian

Daemon ligero de monitoreo y proteccion para el servidor de IA en `/opt/ai`
(Ubuntu Server, RTX 3090, Ryzen 9, Docker/Compose con ComfyUI, Ollama y Open
WebUI). Vigila GPU, sistema, contenedores y servicios HTTP, y puede
ejecutar acciones graduales de proteccion — todas deshabilitadas o en modo
`dry_run` por defecto.

## Arquitectura

```
guardian/
├── main.py           CLI + GuardianDaemon (orquestador: conecta todo lo demas)
├── config.py          Carga y validacion de config.yaml
├── models.py           Dataclasses/enums tipados compartidos
├── logger.py            Logging con rotacion
├── state.py             Persistencia JSON del ultimo estado (escritura atomica)
├── monitors/
│   ├── gpu.py            nvidia-smi -> GpuSample (por GPU)
│   ├── system.py         psutil -> SystemSample (CPU, RAM, swap, disco, uptime)
│   ├── docker.py         docker inspect/stats -> ContainerStatus
│   └── services.py       HTTP -> ServiceCheck (ComfyUI, Ollama, Open WebUI)
├── rules/
│   └── engine.py          ThresholdWatcher (umbral+duracion+histeresis+cooldown) puro, sin efectos secundarios
├── actions/
│   ├── docker_actions.py   stop/restart de contenedores, respeta dry_run
│   ├── system_actions.py   apagado seguro, doble compuerta (config + dry_run)
│   └── notifications.py    Logger + Telegram, con cooldown/deduplicacion
└── utils/
    └── commands.py          Wrapper unico de subprocess (timeout, binario ausente, nunca lanza)
```

Cada ciclo (`GuardianDaemon.evaluate_once`): construye un `Snapshot` (GPU +
sistema + Docker + servicios) → el `RuleEngine` lo evalua y devuelve una
lista de `RuleEvent` (sin tocar Docker ni el sistema) → `GuardianDaemon`
decide, segun la configuracion de acciones y `dry_run`, si registra,
notifica y/o ejecuta una accion → persiste el estado en JSON.

El motor de reglas es intencionalmente puro (no conoce Docker ni
subprocess) para que `test_rule_engine.py` no necesite mocks de sistema.

## Requisitos

- Linux (probado en Ubuntu Server)
- Python 3.11+
- `nvidia-smi` (driver NVIDIA) — opcional: si falta, el monitor de GPU
  degrada a "sin datos" sin detener el daemon
- Docker — opcional: si falta o el daemon no responde, el monitor de
  Docker degrada a "no disponible"
- Acceso de lectura al socket de Docker para el usuario del servicio (ver
  [Riesgos del grupo docker](#riesgos-del-grupo-docker))

## Instalacion

```bash
cd /opt/ai/guardian
sudo ./install.sh
```

El script:

1. Valida Linux, Python >= 3.11, `nvidia-smi` y Docker (no destructivo; si
   falta algo, pregunta antes de continuar).
2. Crea un entorno virtual en `.venv/` e instala dependencias.
3. Crea el usuario de sistema `ai-guardian` (sin login, sin home) si no
   existe.
4. Pregunta explicitamente si quieres agregarlo al grupo `docker`
   (advierte primero sobre las implicaciones — ver mas abajo).
5. Crea `/etc/ai-guardian/` y `/var/log/ai-guardian/`.
6. Copia `config.example.yaml` a `/etc/ai-guardian/config.yaml` **solo si
   no existe ya** (nunca sobreescribe tu configuracion) y crea una
   plantilla `/etc/ai-guardian/guardian.env`.
7. Instala la unidad systemd y corre `daemon-reload`.
8. Valida la configuracion (`ai-guardian validate-config`) — si falla, se
   detiene antes de habilitar nada.
9. Pregunta si quieres habilitar e iniciar el servicio ya mismo.

Para instalar solo en un entorno de desarrollo (sin systemd ni usuario de
sistema):

```bash
cd /opt/ai/guardian
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ai-guardian --config config.example.yaml check
```

## Configuracion

El archivo vive en `/etc/ai-guardian/config.yaml` (parte de
`config.example.yaml` como base). Puntos clave:

- `general.dry_run: true` — **por defecto**. En este modo, toda accion se
  registra como "se habria ejecutado..." pero nunca se ejecuta de verdad.
- `gpu.thresholds` — 3 niveles graduados de temperatura (warning/critical/
  emergency), cada uno con su propia duracion sostenida antes de
  considerarse real, mas `hysteresis_margin_c` (por defecto 5°C: una
  alerta a 84°C no se considera recuperada hasta bajar de 79°C).
- `gpu.actions` — cada accion (`stop_comfyui_on_critical`,
  `stop_ollama_on_emergency`, `shutdown_on_emergency`) esta en `false` por
  defecto y debe habilitarse una por una, explicitamente.
- `docker.monitored_containers` — usa los `container_name` reales
  definidos en `/opt/ai/stacks/*.yaml`: `comfyui`, `ollama`, `open-webui`.
- Secretos (tokens de Telegram) nunca van en el YAML: se declaran solo los
  *nombres* de las variables de entorno (`bot_token_env`, `chat_id_env`) y
  el valor real se pone en `/etc/ai-guardian/guardian.env`.

Claves opcionales adicionales no listadas en el ejemplo (tienen defaults
razonables en codigo si se omiten): `general.state_file`,
`gpu.actions.action_cooldown_seconds` (default 300s), `system.disk_paths`
(default `["/"]`; en este servidor `/mnt/ai-storage` es el mismo
filesystem que `/`).

> Si cambias `logging.file` o `general.state_file` a una ruta fuera de
> `/var/log/ai-guardian`, tambien debes actualizar `ReadWritePaths=` en
> `ai-guardian.service` (el endurecimiento de systemd deja el resto del
> filesystem en solo lectura, `ProtectSystem=strict`) y correr
> `sudo systemctl daemon-reload`.

Valida siempre despues de editar:

```bash
sudo -u ai-guardian /opt/ai/guardian/.venv/bin/ai-guardian --config /etc/ai-guardian/config.yaml validate-config
```

## Comandos

```bash
ai-guardian --config <ruta> check               # una revision, resumen legible
ai-guardian --config <ruta> status               # ultimo estado persistido (JSON)
ai-guardian --config <ruta> run                  # corre el daemon en primer plano
ai-guardian --config <ruta> validate-config      # valida el YAML, lista errores claros
ai-guardian --config <ruta> test-notification    # envia una notificacion de prueba
ai-guardian --config <ruta> metrics              # metricas actuales en JSON
```

Si `--config` se omite, usa `$AI_GUARDIAN_CONFIG` o
`/etc/ai-guardian/config.yaml`.

Bajo systemd, usa el usuario dedicado:

```bash
sudo -u ai-guardian /opt/ai/guardian/.venv/bin/ai-guardian --config /etc/ai-guardian/config.yaml check
```

## Como ejecutar las pruebas

```bash
cd /opt/ai/guardian
.venv/bin/pip install -e ".[dev]"   # si aun no se instalo
.venv/bin/pytest -q
```

Las pruebas usan mocks para `subprocess`/comandos: nunca detienen
contenedores reales ni apagan el servidor.

## Como consultar logs

```bash
sudo systemctl status ai-guardian
sudo journalctl -u ai-guardian -f
tail -f /var/log/ai-guardian/guardian.log
```

Formato de linea: `FECHA NIVEL evento clave=valor clave2=valor2`. Nunca se
registran tokens ni contrasenas.

## Como activar una accion de manera segura

Cada accion tiene su propio interruptor independiente; actívalas una a la
vez y observa unos dias antes de sumar la siguiente:

1. Deja `general.dry_run: true` y activa solo el flag de la accion en
   `/etc/ai-guardian/config.yaml`, por ejemplo:
   ```yaml
   gpu:
     actions:
       stop_comfyui_on_critical: true
   ```
2. Reinicia el servicio y revisa el log: veras
   `[dry_run] se habria ejecutado: docker stop --time 30 comfyui` cuando
   la condicion se cumpla de verdad (umbral + duracion sostenida).
3. Cuando confirmes que el disparo ocurre cuando esperas (y no antes),
   cambia `general.dry_run: false`.
4. `sudo systemctl restart ai-guardian` y confirma con
   `sudo journalctl -u ai-guardian -f`.

Para `shutdown_on_emergency` hay una tercera compuerta: el usuario del
servicio no tiene privilegios para apagar el sistema (ver
[endurecimiento de systemd](#riesgos-del-grupo-docker) —
`CapabilityBoundingSet=` vacio). Habilitarlo de verdad requiere ademas:
editar `ai-guardian.service` para permitir la capacidad necesaria y
configurar una regla `sudoers` minima que autorice unicamente el comando
`shutdown` para el usuario `ai-guardian`. Esto es deliberado: un apagado
automatico del servidor es la accion mas dificil de revertir de todas, y
requiere que rompas tres barreras independientes a proposito.

## Como regresar a dry_run

```yaml
general:
  dry_run: true
```

```bash
sudo systemctl restart ai-guardian
```

Es instantaneo y no requiere deshabilitar las acciones individuales (el
flag global de `dry_run` tiene prioridad: mientras este en `true`, ninguna
accion real se ejecuta sin importar los demas flags).

## Como desinstalar

```bash
cd /opt/ai/guardian
sudo ./uninstall.sh
```

Detiene y deshabilita el servicio, y elimina la unidad systemd
automaticamente. Todo lo demas (entorno virtual, `/etc/ai-guardian`
—incluye secretos—, `/var/log/ai-guardian`, el usuario `ai-guardian`) se
pregunta uno por uno antes de borrarse. El codigo fuente en
`/opt/ai/guardian` no se toca.

## Limitaciones del control de ventiladores en NVIDIA Linux

Esta version **solo lee** `fan.speed` reportado por `nvidia-smi`; no lo
controla. Razones tecnicas por las que el control real es mas delicado de
lo que parece y se dejo fuera a proposito:

- En Linux, controlar la velocidad del ventilador de una GPU NVIDIA vía
  `nvidia-settings` requiere que el driver exponga el control por
  software, lo cual normalmente exige correr un servidor X con
  `Coolbits` habilitado en `xorg.conf` — inviable en un servidor headless
  como este sin complicar la configuracion grafica.
- Algunas RTX serie 30 en modo headless no exponen un control de PWM
  fiable via NVML/`nvidia-smi` sin `Coolbits`.
- Fijar una curva de ventilador incorrecta puede causar sobrecalentamiento
  silencioso si el daemon calcula mal o pierde el proceso de control.

El codigo esta preparado para incorporarlo despues (el `GpuSample` ya trae
`fan_speed_percent`, y el `RuleEngine`/`actions` ya separan "detectar" de
"actuar"), pero requiere una fase aparte con pruebas de hardware.

## CoolBits

`CoolBits` es una opcion del driver propietario de NVIDIA para X.Org que
habilita funciones normalmente ocultas: overclock de GPU/memoria, control
manual de ventilador, y ajustes de voltaje, expuestas via
`nvidia-settings`. Requiere:

- Un `xorg.conf` con la seccion `Device` de NVIDIA y `Option "Coolbits" "N"`.
- Un servidor X corriendo (incluso "sin cabeza", con un monitor dummy o
  `nvidia-xconfig --allow-empty-initial-configuration`).

En un servidor de inferencia sin salida de video, mantener un X corriendo
solo para exponer `Coolbits` agrega superficie de fallo y complejidad de
mantenimiento por un beneficio (control de ventilador) que **esta version
no implementa**. Si en el futuro se agrega, se documentara aqui el
procedimiento exacto y los riesgos de cada nivel de `Coolbits`.

## Riesgos del grupo docker

El socket de Docker (`/var/run/docker.sock`) no distingue "solo lectura"
de "control total": cualquier proceso con acceso a el puede, por ejemplo,
lanzar un contenedor con `-v /:/host` y modificar cualquier archivo del
host como root. **Pertenecer al grupo `docker` equivale en la practica a
tener privilegios de root en la maquina.**

`install.sh` no agrega al usuario `ai-guardian` al grupo `docker`
automaticamente: pregunta primero y muestra esta misma advertencia. Sin
ese acceso, el monitor de Docker seguira funcionando pero reportara
`docker_available: false` (el daemon sigue vivo, solo sin datos de
contenedores).

Alternativas mas restrictivas si esto te preocupa (fuera del alcance de
esta primera version, pero documentadas para referencia futura):

- Un proxy como `docker-socket-proxy` que exponga solo los endpoints de
  lectura (`GET /containers/*`, `GET /info`) y bloquee `POST`/`DELETE`.
- Ejecutar el monitor de Docker con capacidades reducidas via `sudo` con
  una regla que solo permita `docker inspect`/`docker stats`/`docker ps`.

## Solucion de problemas

**`ai-guardian: no se pudo abrir el archivo de log ... Permission denied`**
El proceso no tiene permiso de escritura en `/var/log/ai-guardian`. Bajo
systemd esto no deberia pasar (el directorio se crea con el dueno
correcto en la instalacion); si corres la CLI manualmente como otro
usuario, usa `--config` apuntando a un YAML con `logging.file` en una ruta
escribible, o corre con `sudo -u ai-guardian`.

**`GPU: sin datos (nvidia-smi no disponible o fallo la consulta)`**
Revisa `nvidia-smi` manualmente. Si el binario no existe, el daemon sigue
funcionando (solo sin metricas de GPU); revisa el driver.

**`Docker: NO disponible`**
Verifica `docker info`. Si el usuario `ai-guardian` no esta en el grupo
`docker`, agregalo conscientemente (ver seccion anterior) y reinicia el
servicio.

**El servicio reinicia en bucle**
`sudo journalctl -u ai-guardian -n 100` para ver la excepcion. Si es un
error de configuracion, corre `validate-config` para verlo con mas
detalle antes de que el daemon intente arrancar.

**Una condicion critica no dispara la accion esperada**
Revisa que: `dry_run` este en `false`, el flag de la accion especifica
este en `true`, y que la condicion se haya sostenido por
`*_duration_seconds` completos (una lectura aislada nunca dispara nada,
es deliberado). El log mostrara
`action_suppressed_cooldown` si la accion ya se ejecuto recientemente y
esta en cooldown.

**Quiero confirmar que las notificaciones funcionan**
`ai-guardian --config ... test-notification` — respeta el cooldown de
deduplicacion igual que las notificaciones reales.
