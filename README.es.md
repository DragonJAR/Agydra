# agydra

<p align="center">
  <img src="logo.png" alt="logo de agydra">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/Licencia-MIT-yellow.svg" alt="Licencia: MIT"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Versi%C3%B3n-1.0.0-blue.svg" alt="Versión"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white" alt="Python"></a>
  <img src="https://img.shields.io/badge/Plataformas-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey.svg" alt="Plataformas">
  <a href="https://www.DragonJAR.org"><img src="https://img.shields.io/badge/Autor-DragonJAR-orange.svg" alt="Autor: DragonJAR"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/Read%20in-English-0078D4?logo=readme&logoColor=white" alt="Read in English"></a>
</p>

> **Una sola instalación de `agy`, múltiples cuentas de Google totalmente aisladas.** `agydra` es un lanzador multi-perfil para el CLI `agy` (Google Antigravity) que proporciona a cada perfil su propia sesión OAuth privada mediante un home overlay por perfil. El directorio `~/.gemini` real nunca se modifica y `agy` jamás es interceptado. Construido estrictamente con la librería estándar de Python (**Python ≥ 3.9, cero dependencias de terceros en runtime**): un núcleo limpio y DRY con adaptadores ligeros para macOS, Linux y Windows.

---

## 💡 ¿Por qué Agydra?

El CLI `agy` deriva su almacén de datos (`~/.gemini`) directamente del directorio personal del usuario. Si gestionas múltiples cuentas de Google (por ejemplo personal, corporativa, de clientes o de pruebas), alternar entre ellas normalmente requiere iniciar sesión repetidamente, sobreescribir tokens OAuth y arriesgarte a ejecutar prompts en la cuenta equivocada.

`agydra` resuelve esto limpiamente a nivel del sistema operativo:

```
agy                 → sesión genérica, utiliza el ~/.gemini real (intacto)
agydra -p trabajo … → mismo binario agy, pero HOME apunta al overlay aislado del perfil
```

- **Cero mezcla de credenciales:** Cada perfil autentica en su propio almacén aislado.
- **Ejecución paralela multi-cuenta:** Ejecuta tareas en paralelo entre distintas cuentas de forma simultánea sin conflictos de tokens.
- **Sin parches ni intercepciones binarias:** `agy` se ejecuta sin modificaciones; el aislamiento se logra puramente mediante redirección de entorno y overlays en el sistema de archivos.
- **Bloqueos consultivos a nivel de kernel:** Las sesiones están protegidas por locks de archivo del SO que se liberan automáticamente incluso ante fallos abruptos o reinicios.

---

## ⚡ Inicio Rápido

### Requisitos Previos

| Requisito | Detalle |
|---|---|
| **Python ≥ 3.9** | Solo librería estándar — cero dependencias de terceros en runtime. |
| **CLI `agy`** | Instalado y disponible en el `PATH` (o especificado mediante `-b` / archivo de configuración). |
| **bwrap** *(opcional)* | Solo Linux: sandbox bubblewrap para enmascarar sockets DBus/keyring (`use_linux_sandbox`). |

### 1. Instalación

**Recomendado (Instalación en un solo comando desde el clon):**

```sh
# Clona el repo, crea un venv local, instala el script de consola y coloca un shim en ~/.local/bin
python3 agydra.py
```

*O mediante pip/pipx:*

```sh
pip install .        # o: pipx install .
```

Verifica tu entorno de inmediato:

```sh
agydra doctor
```

### 2. Configuración Inicial (En menos de 60 segundos)

```sh
# 1. Crea tu perfil aislado
agydra create trabajo -d "Cuenta corporativa"

# 2. Completa el flujo OAuth una sola vez (los tokens se guardan en el almacén de 'trabajo')
agydra login trabajo

# 3. Lanza agy con tu nuevo perfil
agydra -p trabajo "Explica la computación cuántica en tres frases"
```

---

## 🚀 Flujos de Trabajo Principales

### 1. Aislamiento por Proyecto sin Fricción (`agydra use`)
Olvídate de pasar `-p` en cada comando. Fija un perfil a un repositorio o carpeta una sola vez:

```sh
cd ~/proyectos/pasarela-pagos
agydra use cliente-acme

# Todos los comandos agydra dentro de este árbol usarán automáticamente 'cliente-acme'
agydra "Resume los commits recientes"
agydra status
```

### 2. Pool de Cuentas y Ejecución Concurrente (`agydra -r`)
Al orquestar agentes automáticos o scripts en segundo plano entre varias terminales, usa `-r` (`--random`) para seleccionar automáticamente el perfil autenticado, desocupado y menos recientemente usado:

```sh
# Terminal 1: Toma el perfil 'alfa'
agydra -r "Ejecutar pruebas de integración"

# Terminal 2: Toma automáticamente el perfil 'beta' (salta el ocupado 'alfa')
agydra -r "Revisar hallazgos de seguridad"
```

### 3. Inspección de Cuotas en Tiempo Real (`agydra usage`)
Supervisa cuotas de modelos y límites de tasa en todas tus cuentas sin necesidad de abrir sesiones interactivas:

```sh
# Vista global resumida de todas las cuentas
agydra usage

# Desglose detallado para un perfil específico con temporizadores de reinicio
agydra usage trabajo
```

*Nota:* `agydra usage` es de solo lectura y puede ejecutarse de forma segura junto a sesiones activas.

### 4. Compartir Configuración sin Fugas de Secretos (`agydra share-config`)
Distribuye herramientas personalizadas y definiciones MCP entre cuentas sin transferir secretos OAuth:

```sh
# Copia únicamente settings.json y mcp.json; las credenciales permanecen estrictamente privadas
agydra share-config trabajo personal pruebas
```

### 5. Verificación en Seco (`agydra -n`)
Inspecciona variables de entorno, redirecciones del sistema de archivos y argumentos sin ejecutar ninguna acción:

```sh
agydra -np trabajo "¿Cuál es mi entorno actual?"
```

---

## 🧭 Referencia de Comandos y Parámetros

### Subcomandos y Alias

Cualquier subcomando de gestión acepta también la sintaxis `--NOMBRE` o `-NOMBRE` (por ejemplo `agydra --list` == `agydra list`).

| Comando | Alias | Descripción |
|---|---|---|
| `agydra [FLAGS] <args de agy...>` | — | Lanzador por defecto: ejecuta `agy` con el perfil resuelto. |
| `agydra list` | `ls`, `l` | Muestra tabla de perfiles: número, email, marca default, estado de auth, ocupado, último uso. |
| `agydra create NOMBRE [-d DESC]` | `c` | Crea el almacén de un perfil aislado. |
| `agydra login [NOMBRE\|#] [-f] [-n]` | `in` | Corre el flujo OAuth de `agy` aislado a ese perfil (`-f` fuerza re-login; `-n` dry-run). |
| `agydra import NOMBRE\|# [-s DIR]` | `imp` | **Copia** (nunca mueve) un `~/.gemini` existente a un perfil (`-s` sobreescribe el directorio origen). |
| `agydra status [-n]` | `st` | Muestra perfil activo, razón de resolución, binario, email, estado de auth y lock. |
| `agydra default [NOMBRE\|#]` | `d` | Consulta o establece el perfil por defecto global. |
| `agydra use NOMBRE\|#` | `u` | Escribe un marcador `.agydra` fijando el perfil al directorio actual. |
| `agydra rename A B` | `mv` | Renombra un perfil y actualiza referencias por defecto (rechaza perfiles ocupados). |
| `agydra delete NOMBRE\|# [-f] [--no-backup]` | `rm` | Crea un respaldo de seguridad ZIP en `backups/` y elimina el perfil (rechaza ocupados). |
| `agydra share-config ORIGEN DESTINO...` | `share` | Copia de forma segura `settings.json` y `mcp.json` desde `ORIGEN` (nunca credenciales). |
| `agydra setup [-n]` | `install` | Instalador idempotente: valida venv, instala script de consola y configura shim en PATH. |
| `agydra doctor [--fix] [-f]` | `doc` | Suite de diagnósticos. `--fix` repara automáticamente enlaces rotos, locks huérfanos y slots obsoletos. |
| `agydra usage [NOMBRE\|#]` | `us` | Inspecciona cuotas en vivo y estados de límite de tasa entre cuentas. |

### Parámetros del Lanzador

Los flags del lanzador deben especificarse **antes** de los argumentos destinados a `agy`:

| Flag Corto | Flag Largo | Descripción |
|:---:|---|---|
| `-p NOMBRE\|#` | `--profile` | Perfil objetivo por nombre o número 1-based (de `agydra list`). |
| `-r` | `--random` | Elige automáticamente el perfil libre, autenticado y menos usado (requiere 2+ perfiles). |
| `-n` | `--dry-run` | Imprime el plan de lanzamiento, rutas y entorno sin ejecutar `agy`. |
| `-b RUTA` | `--binary` | Sobreescribe la ruta del ejecutable `agy` para esta llamada. |
| `-f` | `--force` | Omite la adquisición del lock de sesión; permite ejecuciones paralelas o de emergencia sobre perfiles ocupados. |

### 🔀 Combinaciones Comunes de Parámetros

Los flags cortos se pueden agrupar según las convenciones estándar POSIX (`-nr` == `-n -r`):

| Combinación | Equivalencias | Propósito |
|---|---|---|
| `agydra -p trabajo "prompt"` | `--profile=trabajo`, `-ptrabajo` | Lanza `agy` con un perfil explícito. |
| `agydra -r "prompt"` | `--random` | Toma automáticamente una cuenta libre y autenticada del pool. |
| `agydra -rf "prompt"` | `-r -f`, `--random --force` | Elige perfil libre; si todos están ocupados o solo hay 1 perfil, fuerza el lanzamiento. |
| `agydra -np trabajo` | `-n -p trabajo`, `--dry-run -p trabajo` | Inspecciona el plan de lanzamiento (rutas, entorno, binario) sin ejecutar nada. |
| `agydra -nr` | `-n -r`, `--dry-run --random` | Visualiza qué perfil libre sería seleccionado sin ejecutar `agy`. |
| `agydra -fp trabajo` | `-f -p trabajo`, `--force -p trabajo` | Omite el lock de sesión para una tarea paralela urgente o tras una caída previa. |
| `agydra -b /ruta/agy -p trabajo` | `--binary /ruta/agy -p trabajo` | Prueba un binario experimental de `agy` con credenciales aisladas. |
| `agydra doctor --fix -f` | `agydra doc --fix --force` | Ejecuta autoreparación de perfiles colgados, locks y enlaces huérfanos sin confirmación interactiva. |
| `agydra delete antiguo -f` | `agydra rm antiguo --force` | Borrado no interactivo (genera respaldo ZIP primero; sigue rechazando perfiles ocupados). |

### Cascada de Resolución de Perfiles

Al lanzar sin `-p`, `agydra` resuelve el perfil activo de forma determinista (la primera coincidencia gana):

```
1. Flag --profile / -p
   └── 2. Variable de entorno AGYDRA_PROFILE
       └── 3. Archivo marcador .agydra (directorio ancestro más cercano al CWD; anula -r)
           └── 4. default_profile configurado en agydra.json
               └── 5. Primer perfil en orden alfabético
```

**Códigos de Salida:** `0` Éxito · `1` Error general o fallo de diagnóstico · `2` Conflicto de flags (`-p` + `-r`) · `126` Binario no ejecutable · `127` Binario no encontrado · `130` Interrumpido con Ctrl-C.

---

## 🛡️ Arquitectura e Invariantes

```
Home del Host (~/)
├── .gitconfig, .ssh, .bashrc (compartidos de forma nativa vía enlaces/junctions)
└── ~/.gemini (datos genéricos, INTACTOS)

Almacén Agydra (<store root>/)
├── profiles/
│   ├── trabajo/data/     <── Almacenamiento real de tokens OAuth y settings de 'trabajo'
│   └── personal/data/    <── Almacenamiento real de tokens OAuth y settings de 'personal'
└── overlays/
    └── trabajo/          <── HOME inyectado durante la ejecución de agydra
        ├── .gemini       ───> symlink hacia profiles/trabajo/data
        └── (symlinks hacia herramientas del host: .gitconfig, .ssh, ...)
```

### 1. Mecánica del Home Overlay
- `agydra` crea una estructura aislada en `<store>/overlays/<perfil>`.
- `<overlay>/.gemini` enlaza directamente a `<store>/profiles/<perfil>/data`.
- Las entradas principales de configuración del usuario (`.ssh`, `.gitconfig`, entornos de terminal) se reflejan mediante symlinks (o junctions en Windows), asegurando que las herramientas de desarrollo funcionen con total normalidad.
- Los directorios ancestros de la raíz del almacén (como `~/Library` en macOS) se reflejan como directorios reales para que el almacén mismo permanezca inaccesible desde el overlay.
- Se inyecta `AGYDRA_REAL_HOME` en el proceso hijo, permitiendo que subshells y comandos secundarios ubiquen el home real del sistema.

### 2. Bloqueos Consultivos a Nivel de Kernel
- El control de concurrencia utiliza locks de archivo del SO en `<store>/locks/<perfil>.lock` (`fcntl.flock` en POSIX, `msvcrt.locking` en Windows).
- **Cero bloqueos huérfanos:** El bloqueo está ligado al descriptor de archivo del proceso activo. Si el proceso termina (de forma normal, anormal o por `SIGKILL`), el kernel del sistema operativo libera el lock de inmediato.

### 3. Adaptadores Multiplataforma

| Plataforma | Mecanismo | Detalle |
|---|---|---|
| **macOS** | Puente de Keychain | Enlaza el servicio fijo `antigravity` de `agy` con slots privados por perfil (`agydra.<perfil>`). Intercambia credenciales en el slot activo para la sesión y las restaura al salir. Todas las escrituras validan la identidad contra el email conocido del perfil. |
| **Linux** | Sandbox bwrap | Sandboxing opcional con bubblewrap (`use_linux_sandbox=true`) para enmascarar sockets DBus/keyring y forzar aislamiento estricto en disco. Degrada limpiamente con una advertencia si `bwrap` no está instalado. |
| **Windows** | Junctions Nativas | El reflejo de directorios aprovecha junctions NTFS (`mklink /J`) y llamadas estándar `Path.unlink()` sin requerir permisos de Administrador ni Modo Desarrollador. |

---

## 🩺 Diagnóstico y Salud (`agydra doctor`)

La suite de diagnósticos ejecuta 10 comprobaciones exhaustivas en una sola pasada:

```sh
agydra doctor
```

```text
agydra doctor — agydra 1.0.0 on darwin
legend: [ok]=pass [!!]=warn [XX]=fail
[ok] agy binary: /usr/local/bin/agy
[ok] store writable: ~/Library/Application Support/agydra
[ok] profiles: 3
  - trabajo: authenticated
  - personal: authenticated
  - pruebas: not-authenticated
[ok] locks: no live sessions
[ok] isolation: verified
[ok] keychain bridge: functional
[ok] profile stores contain agy data layout (schema canary passed)
[ok] store clean (no orphaned artifacts)
[ok] linux sandbox: n/a (not linux)
[ok] install: shim valid at ~/.local/bin/agydra

result: healthy
```

**Reparación Automática:**
```sh
agydra doctor --fix
```
Limpia automáticamente overlays huérfanos, purga locks colgados, elimina slots obsoletos del llavero y reconecta enlaces rotos.

---

## ⚙️ Referencia de Configuración

La configuración global reside en `<store root>/agydra.json`:

| Sistema Operativo | Ubicación por Defecto del Almacén |
|---|---|
| **macOS** | `~/Library/Application Support/agydra` |
| **Linux** | `$XDG_DATA_HOME/agydra` o `~/.local/share/agydra` |
| **Windows** | `%LOCALAPPDATA%\agydra` |

*Sobreescribir con:* `export AGYDRA_HOME=/ruta/personalizada`

```json
{
  "default_profile": "trabajo",
  "settings": {
    "use_linux_sandbox": false,
    "copy_settings_on_create": true,
    "windows_redirect_home": false
  },
  "agy_binary": "/usr/local/bin/agy"
}
```

### Variables de Entorno

| Variable | Propósito |
|---|---|
| `AGYDRA_PROFILE` | Establece el perfil activo por defecto (anulado por `-p` y por el marcador `.agydra`). |
| `AGYDRA_HOME` | Directorio personalizado para el almacén de perfiles y overlays. |
| `AGYDRA_AGY_BIN` | Ruta explícita al ejecutable `agy`. |
| `AGYDRA_NO_KEYCHAIN` | Desactiva el intercambio de llavero en macOS (solo archivos de tokens en disco). |
| `NO_COLOR` / `FORCE_COLOR` | Controla los colores ANSI en la terminal. |

---

## 🔧 Estructura del Proyecto y Pruebas

```text
agydra/                     # Estructura plana (cero dependencias de terceros)
├── agydra.py               # Punto de entrada para bootstrap y definición de versión
├── models.py               # Modelos de datos Profile y Config
├── platforms.py            # Detección de rutas por SO, descubrimiento de binarios y ejecución
├── ui.py                   # Formato ANSI en terminal y estilos de salida
├── banner.py               # Renderizado del logo en terminal
├── store.py                # CRUD de perfiles, operaciones atómicas y respaldos ZIP
├── locks.py                # Bloqueos de sesión a nivel de kernel (flock / msvcrt)
├── resolver.py             # Cascada determinista de resolución y evaluación de marcadores
├── account.py              # Parseo de tokens OAuth y detección de identidad
├── keychain.py             # Puente de llavero por perfil en macOS
├── isolation.py            # Construcción de home overlays y redirección de entorno
├── runner.py               # Orquestación de lanzamiento: resolver → planear → overlay → ejecutar
├── doctor.py               # Suite de diagnóstico y motor de autoreparación
├── cli.py                  # Analizador de argumentos CLI y despachador de comandos
├── vocab.py                # Vocabulario de comandos y nombres de perfil reservados
├── orphans.py              # Auditoría inversa del almacén y limpiador de huérfanos
├── usage.py                # Inspector de cuotas y parseador de límites de tasa
├── bootstrap.py            # Instalador idempotente de venv y shim en PATH
├── tests/                  # 525 pruebas automáticas unitarias y de integración
├── README.md               # Documentación en inglés
├── README.es.md            # Documentación en español
├── AGENTS.md               # Convenciones de desarrollo e invariantes arquitectónicos
├── LICENSE                 # Licencia MIT
└── pyproject.toml          # Metadatos del empaquetado
```

### Ejecución de Pruebas

La suite de pruebas valida entornos simulados, casos límite y todas las plataformas sin tocar los archivos del sistema:

```sh
python3 -W error::ResourceWarning -m pytest tests/ -q
# 522 passed, 3 skipped (en macOS) en ~40s (0 advertencias)

python3 -m unittest discover -s tests -q
# Ran 525 tests en ~36s - OK
```

---

## ⚠️ Limitaciones

- `agydra` gestiona **perfiles y sesiones**, no la instalación ni las actualizaciones del binario `agy`.
- El aislamiento depende de que `agy` derive su directorio de configuración a partir de `HOME` (o `USERPROFILE`). Si una versión futura de `agy` cambia este comportamiento, el canario de esquema de `agydra doctor` lo detecta inmediatamente y te avisa.
- Las ramas específicas de Windows se verifican en Windows nativo; en sistemas Unix se validan mediante la suite de pruebas unitarias aisladas.

---

## 🤝 Contribuir

¡Agradecemos las contribuciones de la comunidad! Por favor respeta nuestros invariantes centrales:
1. **Cero dependencias de terceros en runtime:** Usa estrictamente la librería estándar de Python (`sys`, `os`, `pathlib`, `fcntl`, `tempfile`, etc.).
2. **Paridad multiplataforma:** Los cambios deben ejecutarse de forma idéntica en macOS, Linux y Windows.
3. **Verificación exhaustiva:** Ejecuta los 525 tests antes de abrir un pull request.
4. **Coherencia arquitectónica:** Revisa [AGENTS.md](AGENTS.md) para conocer las directrices completas.

---

## 📄 Licencia

Distribuido bajo la **Licencia MIT**. Consulta [LICENSE](LICENSE) para más detalles.

## 👨💻 Autor

Desarrollado y mantenido por **[DragonJAR](https://www.DragonJAR.org)** — Seguridad, Comunidad y Herramientas Libres.

---

*Aviso: `agydra` es un proyecto comunitario independiente y no está afiliado, respaldado ni patrocinado por Google. `agy` y Antigravity son marcas registradas de Google LLC. Utiliza tus cuentas y tokens cumpliendo los Términos de Servicio de Google.*
