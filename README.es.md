# agydra

<p align="center">
  <img src="logo.png" alt="logo de agydra" width="180">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/Licencia-MIT-yellow.svg" alt="Licencia: MIT"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Versi%C3%B3n-1.1.0-blue.svg" alt="Versión"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white" alt="Python"></a>
  <img src="https://img.shields.io/badge/Plataformas-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey.svg" alt="Plataformas">
  <a href="https://www.DragonJAR.org"><img src="https://img.shields.io/badge/Autor-DragonJAR-orange.svg" alt="Autor: DragonJAR"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/Read%20in-English-0078D4?logo=readme&logoColor=white" alt="Read in English"></a>
</p>

> **Lanzador multi-perfil para el CLI `agy` (Google Antigravity) con sesiones OAuth totalmente aisladas por perfil.** Una única instalación de `agy`, muchas cuentas de Google con sesión independiente en la misma máquina, sobre **macOS, Linux y Windows** desde una sola base de código. `agydra` está construido solo con la librería estándar de Python (cero dependencias de terceros en runtime), no intercepta `agy` jamás y nunca toca tu `~/.gemini` real: cada perfil autentica en su almacén privado mediante un home overlay.

## 🎯 Qué hace esta herramienta

- **Muchas cuentas, un solo binario.** Lanza el mismo `agy` con la sesión aislada del perfil que elijas, sin duplicar instalaciones ni tocar la sesión genérica.
- **Aislamiento real.** Cada perfil autentica en su propio almacén privado; el `~/.gemini` real permanece intacto y `agy` nunca se intercepta ni se modifica.
- **Multiplataforma.** macOS, Linux y Windows desde una única base de código solo-stdlib, sin `sudo` ni permisos de administrador.
- **Sesiones paralelas.** Locks mantenidos por el kernel (POSIX `flock` / Windows `msvcrt`) evitan sesiones huérfanas y permiten correr varios perfiles a la vez.
- **Resolución determinista.** Cascada flag → entorno → marcador de proyecto → default → primer perfil, siempre con la razón visible en `status`.

```
agy                 → sesión genérica, conserva el ~/.gemini real (intacto)
agydra -p trabajo ... → mismo binario agy, pero HOME apunta al overlay del perfil
```

El mecanismo: `agy` deriva todo su directorio de datos (`~/.gemini`) desde la variable de home que ve en ejecución (`HOME` en POSIX, `USERPROFILE` en Windows). Por lanzamiento, `agydra` construye un **home overlay por perfil**: `<overlay>/.gemini` enlaza al almacén privado del perfil, el resto de entradas del home real se reflejan con enlaces, y `agy` se lanza con la variable de home redirigida. Nada más cambia.

## 📦 Instalación

**Opción 1 — instalar con pip o pipx** (desde un checkout de este repositorio):

```sh
pip install .      # o: pipx install .
```

**Opción 2 — ejecutar sin instalar** (solo Python 3.9+):

```sh
python3 -m agydra --help
```

## ⚡ Instalación en un comando

Dos puntos de entrada equivalentes:

```sh
python3 agydra.py    # desde un clon limpio: sin instalar nada, delega en el paquete
agydra setup         # una vez instalado (alias: agydra install)
```

El setup es idempotente (puede re-ejecutarse sin riesgo):

- Valida Python ≥ 3.9.
- Crea `<repo>/.venv` y ejecuta `pip install -e .` dentro (solo metadatos locales, sin descargas de red).
- Escribe un shim administrado en `~/.local/bin/agydra` para que `agydra` funcione desde cualquier directorio.
- Verifica la instalación ejecutando `agydra --version` y sugiere `agydra doctor`.

En Windows no hay shim: el setup imprime el directorio `Scripts` del venv que debe estar en el `PATH`. Con `agydra setup -n` (`--dry-run`) imprimes el estado actual de la instalación (venv, console script, shim, `PATH`) sin tocar nada.

> **Seguridad:** el setup se niega a sobrescribir un archivo ajeno en `~/.local/bin/agydra` (solo reescribe shims que llevan el marcador "Managed by agydra setup"), refresca automáticamente un shim obsoleto y recrea un venv al que le falte el intérprete. `agydra doctor` ahora incluye un check `install` que cubre venv + console script + shim + `PATH`.

## ⚙️ Requisitos previos

| Requisito | Detalle |
|---|---|
| Python ≥ 3.9 | Solo librería estándar — cero dependencias de terceros en runtime. |
| CLI `agy` | Ya instalado y en `PATH`, o indicado con `-b RUTA`, `agy_binary` en `agydra.json` o la variable `AGYDRA_AGY_BIN`. |
| `bwrap` (opcional) | Solo Linux: endurece el aislamiento enmascarando sockets DBus/keyring si `use_linux_sandbox=true`. Si falta, `agydra` degrada con aviso. |

**Verificación rápida del entorno:**

```sh
agydra doctor
```

```text
agydra doctor — agydra 1.2.0 on darwin
legend: [ok]=pass [!!]=warn [XX]=fail
[ok] agy binary: /usr/local/bin/agy
[ok] store writable: ~/Library/Application Support/agydra
[ok] profiles: 2
...
result: healthy
```

## 🚀 Ejemplos de uso

**1. Crear el primer perfil, autenticarlo y lanzarlo**

```sh
agydra create trabajo -d "Cuenta laboral"
agydra login trabajo
agydra -p trabajo "resume la reunión de ayer"
```

```text
created profile: trabajo
authenticate it with: agydra login trabajo
launching agy for login under profile 'trabajo'...
complete the OAuth flow in the browser; tokens land in the profile store
```

**2. Fijar un perfil por proyecto con `use`**

```sh
cd ~/proyectos/api-pagos
agydra use trabajo
agydra status
```

```text
pinned /Users/tu/api-pagos/.agydra -> profile 'trabajo'
agy launches in this directory will use 'trabajo' automatically
profile   : trabajo
reason    : project marker /Users/tu/api-pagos/.agydra
binary    : /usr/local/bin/agy
email     : tu@empresa.com
auth      : authenticated
busy      : no
store     : ~/Library/Application Support/agydra/profiles/trabajo/data
```

**3. Sesiones paralelas con `-r` (perfil libre autenticado)**

```sh
agydra -r "revisa el error del despliegue"    # terminal 1
agydra -r "genera tests del módulo pagos"     # terminal 2
agydra list
```

```text
#  PROFILE    EMAIL             AUTH           DEFAULT  BUSY  LAST USED
1  trabajo    tu@empresa.com    authenticated  *        yes   2026-01-02T10:00:00Z
2  personal   tu@gmail.com      authenticated           yes   2026-01-02T10:00:05Z
```

`-r` exige 2+ perfiles y elige el perfil libre autenticado menos usado; nunca lanza uno ocupado ni sin autenticar.

**4. Inspeccionar el plan de lanzamiento con `--dry-run`**

```sh
agydra -n -p trabajo "resume la reunión"
```

```text
profile : trabajo (flag --profile=trabajo)
binary : /usr/local/bin/agy
argv    : /usr/local/bin/agy resume la reunión
overlay : ~/Library/Application Support/agydra/overlays/trabajo
env     : HOME=~/Library/Application Support/agydra/overlays/trabajo
sandbox : off
```

**5. Compartir settings entre perfiles (nunca credenciales)**

```sh
agydra share-config trabajo personal
```

```text
copied: personal/settings.json
copied: personal/mcp.json
```

## 📖 Capacidades

**Gestión de perfiles**

| Capacidad | Detalle |
|---|---|
| Crear y autenticar | `create` crea el almacén del perfil; `login` corre el OAuth de `agy` aislado a ese perfil. |
| Importar sesión genérica | `import NOMBRE` **copia** (nunca mueve) el `~/.gemini` genérico a un perfil; la fuente se autodetecta y `-s DIR` la sobrescribe (pasar una ruta como NOMBRE se rechaza con guía correcta). |
| Renombrar y borrar | `rename` actualiza la referencia default; `delete` crea un ZIP de respaldo antes de borrar (conserva 5). Ambos rechazan perfiles ocupados. |
| Fijar por proyecto | `use` escribe el marcador `.agydra` para fijar el perfil de ese directorio. |
| Inspección sin efectos | `list` y `status` muestran número, email, estado de auth, ocupado y último uso sin tocar el sistema de archivos. |
| Compartir settings | `share-config` copia solo `settings.json` + `mcp.json` entre perfiles. |

**Aislamiento y seguridad**

| Capacidad | Detalle |
|---|---|
| Home overlay por perfil | `<overlay>/.gemini` enlaza al almacén privado; el resto del home real se refleja con enlaces. |
| `~/.gemini` real intacto | `agydra` nunca escribe en el directorio de datos genérico ni intercepta `agy`. |
| Locks kernel-held | POSIX `flock` con fd heredable llevado en `execvpe` / Windows `msvcrt` byte-range: sin locks huérfanos; `delete`/`rename` rechazan perfiles ocupados. |
| Escrituras atómicas | Toda escritura de JSON/config usa tmp + fsync + `os.replace`. |
| Config corrupta | Degrada a defaults en lectura y se niega a escribir hasta que la repares. |
| Diagnóstico | `doctor` revisa binario, permisos del almacén, perfiles, locks, aislamiento, canario de esquema y sandbox; sale 1 si algún check falla. |

**Matriz multiplataforma**

| Plataforma | Mecanismo |
|---|---|
| macOS | Home overlay + puente de llavero (keychain): intercambia el slot compartido fijo de `agy` por un slot privado por perfil alrededor de cada lanzamiento y lo restaura después; los fallos son avisos no fatales. |
| Linux | Home overlay + sandbox bwrap (bubblewrap) opcional enmascarando sockets DBus/keyring con `use_linux_sandbox=true`; degrada con aviso si falta `bwrap`. |
| Windows | Home overlay redirigiendo `USERPROFILE`; los enlaces de directorio caen a junctions (`mklink /J`), sin permisos de administrador. |

## 🧭 Comandos

| Comando | Alias | Descripción |
|---|---|---|
| `agydra [-p PERFIL\|#] [-r] [-n] [-b RUTA] <args de agy...>` | — | Lanza `agy` con la sesión aislada del perfil resuelto. Los flags de agydra van **antes** de los args de agy; un `-p` tardío va a `agy` y se imprime un aviso. Los flags cortos se agrupan estilo getopt: `-nr -p work` == `-n -r -p work`. Los nombres de perfil no pueden chocar con un subcomando o alias (`status`, `ls`, `mv`, ...) — el dispatcher los opacaría. |
| `list` | `ls`, `l` | Tabla de perfiles: número, email, default, estado de auth, ocupado, último uso. |
| `create NOMBRE [-d DESC]` | `c` | Crea el almacén de un perfil. |
| `login NOMBRE\|#` | `in` | Corre el OAuth de `agy` aislado a ese perfil. |
| `import NOMBRE` | `imp` | **Copia** (nunca mueve) el `~/.gemini` genérico a un perfil. |
| `status [-n]` | `st` | Perfil resuelto + razón + binario + email + auth + ocupado; cero efectos secundarios. |
| `default [NOMBRE\|#]` | `d` | Ver o fijar el perfil default. |
| `use NOMBRE\|#` | `u` | Escribe el marcador `.agydra` fijando el perfil para ese directorio de proyecto. |
| `rename A B` | `mv` | Renombra un perfil (rechaza ocupados). |
| `delete NOMBRE\|# [-f] [--no-backup]` | `rm` | ZIP de respaldo y borrar (rechaza ocupados). |
| `share-config ORIGEN DESTINO...` | `share` | Copia solo `settings.json` + `mcp.json` entre perfiles. |
| `setup [-n]` | `install` | Instalación en un comando: crea el venv, instala el console script y verifica; `-n` imprime el estado actual. `python3 agydra.py` es el bootstrap sin instalación desde un clon limpio. |
| `doctor` | `doc` | Diagnóstico completo del entorno. |

| Flag corto | Flag largo | Función |
|:-:|---|---|
| `-p PERFIL` | `--profile PERFIL` | Elige perfil por nombre o número 1-based. |
| `-r` | `--random` | Perfil libre autenticado menos usado (requiere 2+). |
| `-n` | `--dry-run` | Imprime el plan de lanzamiento sin ejecutar nada. |
| `-b RUTA` | `--binary RUTA` | Sobreescribe el binario `agy`. |

`-p` y `-r` son mutuamente excluyentes. Sin `-p`, la resolución sigue una cascada determinista (gana el primer match): flag `--profile` → variable de entorno `AGYDRA_PROFILE` → archivo marcador `.agydra` (el ancestro más cercano del CWD; lo escribe `agydra use`) → default configurado → primer perfil. El marcador pisa a `-r`. `status` siempre muestra qué perfil se usará desde el directorio actual y por qué.

| Código de salida | Significado |
|:-:|---|
| `0` | Éxito. |
| `1` | Error de ejecución o check de `doctor` fallido. |
| `2` | Conflicto `-p` + `-r`. |
| `126` / `127` | Binario no ejecutable / no encontrado. |
| `130` | Cancelado con Ctrl-C. |

## 🔧 Estructura del proyecto

```
agydra/                  # raíz del repo — layout plano, sin subdirectorio de paquete
├── agydra.py            # VERSION + bootstrap de un comando (python3 agydra.py)
├── models.py            # dataclasses Profile / Config + DEFAULT_SETTINGS
├── platforms.py         # rutas por OS, variable de home, resolución del binario, lanzamiento
├── store.py             # CRUD de perfiles, config, escrituras atómicas, backups
├── locks.py             # locks de sesión kernel-held (flock / msvcrt)
├── resolver.py          # cascada de resolución, perfil libre -r, marcador .agydra
├── account.py           # detección de auth/email desde los datos del perfil
├── keychain.py          # puente de llavero macOS (slot compartido ↔ privado por perfil)
├── isolation.py         # overlay home, enlaces (symlink+junction), env, bwrap
├── runner.py            # build_plan (puro) + run (lock → overlay → keychain → exec)
├── doctor.py            # 8 checks de diagnóstico; exit 1 si alguno FALLA
├── cli.py               # CLI argparse; el lanzador es el modo por defecto
tests/
├── conftest.py          # home falso + binario agy falso + AGYDRA_HOME aislado
└── test_*.py            # ~196 tests (193 pasan + 3 skips de plataforma en macOS)
README.md
README.es.md
AGENTS.md
LICENSE
pyproject.toml
```

El almacén de datos en runtime vive fuera del repo: `%LOCALAPPDATA%\agydra` (Windows), `~/Library/Application Support/agydra` (macOS), `$XDG_DATA_HOME/agydra` o `~/.local/share/agydra` (Linux); la variable `AGYDRA_HOME` lo sobreescribe.

## ⚙️ Configuración

`agydra.json`, en la raíz del almacén:

**Color:** toda la ayuda y el estado se colorean automáticamente en TTY y se
desactivan en pipes/CI. `NO_COLOR=1` fuerza salida plana; `FORCE_COLOR=1` la
fuerza a color.

```json
{
  "default_profile": "work",
  "settings": {
    "use_linux_sandbox": true,
    "copy_settings_on_create": true,
    "windows_redirect_home": false
  },
  "agy_binary": "/absolute/path/to/agy"
}
```

Orden de resolución del binario `agy`: flag `--binary` → `agy_binary` en `agydra.json` → variable de entorno `AGYDRA_AGY_BIN` → búsqueda en `PATH`.

## ⚠️ Limitaciones

- `agydra` no gestiona la instalación, actualización ni renovación de tokens de `agy`: eso queda dentro del directorio de datos de cada perfil, propiedad de `agy`.
- La derivación del home es una propiedad del `agy` actual, no un contrato de API: si una versión futura deja de derivar su directorio de datos desde la variable de home, el canario de esquema de `doctor` lo reportará tras el siguiente `login`.
- Las ramas específicas de Windows (junctions, `msvcrt`, `USERPROFILE`) están implementadas pero solo se ejecutan en Windows real.

## 🤝 Contribuir

- **Solo stdlib.** Cero dependencias de terceros; Python ≥ 3.9. No añadas paquetes.
- **Multiplataforma por construcción.** Cada diferencia de OS vive en `platforms.py` y en los helpers de enlaces de `isolation.py`; nunca ramifiques por SO fuera de esos límites.
- **Tests antes de declarar listo.** Desde la raíz del repo: `python3 -m pytest -q` (102 pasan + 3 skips de plataforma en macOS).
- Consulta `AGENTS.md` para las guías de invariantes, layout y convenciones del proyecto.

## 📄 Licencia

Licencia [MIT](LICENSE). Puedes usar, copiar y modificar este software libremente conservando el aviso de copyright y esta licencia.

## 👨💻 Autor

[![Autor: DragonJAR](https://img.shields.io/badge/Autor-DragonJAR-orange.svg)](https://www.DragonJAR.org)

Creado y mantenido por **[DragonJAR](https://www.DragonJAR.org)** — seguridad, comunidad y herramientas libres.

---

`agydra` es una herramienta comunitaria independiente, sin afiliación ni respaldo de Google. `agy` / Antigravity son productos de Google. Usa tus propias cuentas responsablemente.
