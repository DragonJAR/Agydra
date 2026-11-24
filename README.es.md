# Agydra

<p align="center">
  <img src="logo.png" alt="logo de Agydra">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/Licencia-MIT-blue.svg" alt="Licencia: MIT"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Versi%C3%B3n-1.1.0-blue.svg" alt="Versión"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white" alt="Python"></a>
  <img src="https://img.shields.io/badge/Plataformas-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey.svg" alt="Plataformas">
  <a href="https://www.DragonJAR.org"><img src="https://img.shields.io/badge/author-DragonJAR.org-orange" alt="Author: DragonJAR.org"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/Read%20in-English-0078D4?logo=readme&logoColor=white" alt="Read in English"></a>
</p>

> **Varias cuentas, un solo comando.** Agydra mantiene perfiles separados para **Google Antigravity (`agy`)**, **OpenAI Codex (`codex`)**, **xAI Grok (`grok`)** y **Anthropic Claude Code (`claude`)**. Cada perfil tiene su propio login y su propia configuración, así que puedes cambiar de cuenta, usar varias a la vez o dejar que Agydra elija la que tenga más cuota libre.

Python 3.9+, solo librería estándar. Funciona en macOS, Linux y Windows.

---

## 💡 Por qué

Las CLI oficiales guardan una sola cuenta por equipo (`~/.gemini`, `~/.codex`, `~/.grok`). Si inicias sesión con una segunda cuenta, reemplazas la primera, y dos terminales acaban compartiendo la misma sesión.

Agydra le da a cada perfil su propia carpeta privada y hace que la CLI use esa carpeta:

```
agy                  → tu sesión normal
agydra -p trabajo    → el mismo agy, pero con la cuenta "trabajo"
agydra -r "prompt"   → la siguiente cuenta sin usar, preferiblemente la de más cuota
agydra usage         → la cuota restante de todas las cuentas en una tabla
```

El login siempre lo hace la CLI oficial, y cada token se queda dentro de su perfil.

---

## ⚡ Inicio rápido

### 1. Instalar

```sh
python3 agydra.py      # desde un clon: crea un venv y deja `agydra` en ~/.local/bin
# o
pipx install .         # o: pip install .
agydra doctor          # comprueba que todo esté en orden
```

Instala por tu cuenta los motores que vayas a usar (`agy`, `codex`, `grok`, `claude`). Agydra los busca en el `PATH`, o puedes indicarlos con `-b RUTA` o `AGYDRA_<MOTOR>_BIN`.

### 2. Crear un perfil e iniciar sesión

`agy` es el motor por defecto. Usa `-e` para los demás.

```sh
agydra create trabajo                 # Antigravity
agydra create codex-trabajo -e codex  # Codex
agydra create grok-trabajo  -e grok   # Grok
agydra create claude-trabajo -e claude

agydra login trabajo                  # login oficial, guardado solo en "trabajo"
```

### 3. Usarlo

```sh
agydra -p trabajo "Explica la computación cuántica en tres frases"
agydra -p codex-trabajo --yolo        # lo que va después de los flags lo recibe el motor
agydra list                           # perfiles, cuentas y estado del login
```

---

## 🔄 Rotar entre cuentas

Crea un perfil por cuenta y deja que Agydra elija:

```sh
agydra create fam-main && agydra login fam-main
agydra create fam-dev  && agydra login fam-dev

agydra -r "Refactoriza el middleware de autenticación"   # cualquier motor
agydra -e grok -r "Haz una revisión de seguridad"          # solo perfiles de Grok
```

Cómo elige `-r`:

1. Usa cada perfil con sesión iniciada una vez antes de repetir.
2. Entre los no usados prefiere uno sin sesión abierta y, después, el de más cuota libre.
3. Cuando ya usó todos, prefiere el de más cuota.

La cuota sale del último `agydra usage`; las lecturas que faltan o están desactualizadas van al final, así que ejecuta `agydra usage` de vez en cuando. La rotación es por perfil, no por correo.

```text
agydra usage                                     4 profiles · Sun Sep 27 · 23:18

■ ANTIGRAVITY                GEMINI                  CLAUDE + GPT

 #   PROFILE   ACCOUNT        AVAILABLE  WK · 5H      AVAILABLE  WK · 5H
 1   neta      agent.bot@g…   ████░ 85    85 ·  94    ██░░░ 46    46 · 100
 2   vacan     deep.res@gm…   ████░ 73    73 ·  99    █████ 100  100 · 100

■ OPENAI CODEX

 #   PROFILE   ACCOUNT        AVAILABLE  WK · 5H      ↻        PLAN
 3   codex     lead.dev@gm…   █████ 99    99 · 100    5d 5h    ChatGPT Plus

■ XAI GROK

 #   PROFILE   ACCOUNT        AVAILABLE  ↻        PLAN
 4   grok-work corp.ops@xa…   ████░ 82    6d 2h    SuperGrok

▸ USE NOW   Gemini → neta 85%   Claude/GPT → vacan 100%   Codex → codex 99%   Grok → grok-work 82%
```

`agydra usage <perfil>` muestra el detalle y las horas de reinicio de un perfil. `usage` solo lee, así que puedes ejecutarlo con sesiones abiertas.

---

## 🧰 Tareas habituales

| Quiero… | Comando |
|---|---|
| Usar siempre un perfil dentro de la carpeta de un proyecto | `agydra use cliente-acme` (crea un archivo `.agydra`; `-r` lo ignora) |
| Copiar la configuración de un perfil a otros, sin el login | `agydra share-config trabajo personal staging` |
| Ver qué se ejecutaría, sin ejecutarlo | `agydra -np trabajo` |
| Pasarle al motor un flag que Agydra también usa (como `-r` o `-c`) | `agydra -p claude-trabajo -- -r` |
| Cambiar el idioma | `agydra lang es` (o `AGYDRA_LANG=es` para una sola ejecución) |
| Respaldar y borrar un perfil | `agydra delete trabajo` (guarda un ZIP en `backups/`) |
| Reparar un almacén dañado | `agydra doctor --fix` |

### Claude Code

Claude Code guarda su propio login (incluido el Llavero de macOS). Agydra le da a cada perfil de Claude su propia carpeta de configuración y no toca tu HOME real.

```sh
agydra create claude-trabajo -e claude
agydra login claude-trabajo           # ejecuta `claude auth login`
agydra usage claude-trabajo           # ejecuta el `claude -p "/usage"` del propio perfil
```

Si la lectura en vivo no está disponible, `usage` puede recurrir a una instantánea local. Para activarla, ejecuta `agydra usage --claude-settings claude-trabajo --apply`. Agydra añade entonces un `statusLine` al `settings.json` de ese perfil, y no toca un `statusLine` distinto que ya exista. Los perfiles de Claude no se pueden importar, exportar ni compartir con `share-config`.

---

## 🧭 Referencia de comandos

Un perfil se indica por su nombre o por su número en `agydra list`. Cada comando también funciona como `--comando` (`agydra --list`).

| Comando | Alias | Qué hace |
|---|---|---|
| `list` | `ls`, `l` | Perfiles con número, cuenta, estado del login, sesiones y motor. |
| `create NOMBRE [-e MOTOR] [-d DESC]` | `c` | Perfil nuevo. Motores: `agy` (por defecto), `codex`, `grok`, `claude`. |
| `login [NOMBRE] [-f]` | `in` | Inicia sesión con el flujo oficial del motor. `-f` vuelve a iniciarla. |
| `status [NOMBRE]` | `st` | Motor, binario, cuenta, estado del login y sesiones abiertas. |
| `usage [NOMBRE]` | `us` | Cuota restante de todos los motores. |
| `use NOMBRE` | `u` | Fija la carpeta actual a un perfil. |
| `default [NOMBRE]` | `d` | Muestra o cambia el perfil por defecto. |
| `rename VIEJO NUEVO` | `mv` | Renombra un perfil que no esté en uso. |
| `delete NOMBRE [-f] [--no-backup]` | `rm` | Hace un ZIP de respaldo y luego borra. |
| `import NOMBRE [-s DIR]` | `imp` | Copia un `~/.gemini`, `~/.codex` o `~/.grok` existente a un perfil. |
| `export NOMBRE [-o RUTA]` | `exp` | ZIP portable de un perfil, sin logins ni secretos. |
| `share-config ORIGEN DESTINO...` | `share` | Copia `settings.json`, `mcp.json` y `config.toml`; los logins no se mueven. |
| `doctor [--fix [-f]]` | `doc` | Revisiones de salud; `--fix` repara lo que puede reparar sin riesgo. |
| `setup [-n] [-f]` | `install` | Instala o actualiza el comando `agydra`. |
| `language [en\|es]` | `lang`, `idioma`, `locale` | Muestra o cambia el idioma. |
| `help` | `h` | Ayuda; `agydra COMANDO --help` explica un comando. |
| `version` | | Versión y autor. |

Los flags de lanzamiento van **antes** de los argumentos del motor:

| Flag | Qué hace |
|---|---|
| `-p NOMBRE` | Usa este perfil. |
| `-r` | Rota al mejor perfil disponible. No se combina con `-p`. |
| `-e MOTOR` | Elige el motor (`agy`, `codex`, `grok`, `claude`); con `-r`, rota solo dentro de él. |
| `-n` | Simulación: muestra el plan y no lanza nada. |
| `-b RUTA` | Usa un binario concreto del motor. |
| `-f` | Ignora el límite opcional de sesiones. |
| `--lang CÓDIGO` | Cambia el idioma (`en`, `es`). |

Sin `-p`, Agydra elige el perfil en este orden: `AGYDRA_PROFILE` → el archivo `.agydra` más cercano → `default_profile` → el primer perfil.

**Códigos de salida:** `0` bien · `1` error · `2` uso incorrecto · `126` binario no ejecutable · `127` binario no encontrado · `130` Ctrl-C.

---

## ⚙️ Configuración

Todo vive en una carpeta de almacén. Puedes moverla con `AGYDRA_HOME`.

| SO | Almacén por defecto |
|---|---|
| macOS | `~/Library/Application Support/agydra` |
| Linux | `$XDG_DATA_HOME/agydra` o `~/.local/share/agydra` |
| Windows | `%LOCALAPPDATA%\agydra` |

Los ajustes están en `<almacén>/agydra.json` (todas las claves son opcionales):

```json
{
  "default_profile": "trabajo",
  "settings": {
    "lang": "auto",
    "max_sessions_per_profile": null,
    "copy_settings_on_create": true,
    "use_linux_sandbox": false,
    "windows_redirect_home": false
  },
  "agy_binary": "/usr/local/bin/agy"
}
```

| Variable | Para qué sirve |
|---|---|
| `AGYDRA_HOME` | Ubicación del almacén. |
| `AGYDRA_PROFILE` | Perfil por defecto (Agydra la define dentro de sus sesiones). |
| `AGYDRA_LANG` | Idioma para una sola ejecución. |
| `AGYDRA_AGY_BIN`, `AGYDRA_CODEX_BIN`, `AGYDRA_GROK_BIN`, `AGYDRA_CLAUDE_BIN` | Binario del motor. |
| `AGYDRA_NO_KEYCHAIN` | Desactiva el puente con el Llavero de macOS. |
| `NO_COLOR`, `FORCE_COLOR` | Activa o desactiva los colores. |

---

## 🛡️ Cómo funciona el aislamiento

Cada perfil tiene una carpeta privada con su propio login y su configuración. Al lanzarlo, Agydra hace que el motor use esa carpeta:

| Motor | Apunta a | Tus archivos reales |
|---|---|---|
| `agy` | `HOME` → una copia espejo de tu home para ese perfil | `~/.gemini` no se toca |
| `codex` | `CODEX_HOME` | `~/.codex` no se toca |
| `grok` | `GROK_HOME` y su propio socket líder | `~/.grok` no se toca |
| `claude` | `CLAUDE_CONFIG_DIR` (conserva el HOME real) | tu configuración de Claude no se toca |

- Tus archivos `.ssh`, `.gitconfig` y de la shell siguen visibles dentro del perfil.
- El sistema operativo libera los bloqueos al terminar una sesión, incluso si se cuelga.
- En macOS, `agy` guarda su login en un único espacio compartido del Llavero. Cuando eliges un perfil con `-p`, Agydra pone en ese espacio el login de ese perfil. Las sesiones rotadas (`-r`) usan en cambio el archivo de token de cada perfil, así que varias cuentas pueden funcionar a la vez.
- En Linux, `use_linux_sandbox` puede ocultar el llavero del escritorio mediante `bwrap`. En Windows, Agydra usa junctions de NTFS y no necesita permisos de administrador.

Los detalles internos y las invariantes están en [AGENTS.md](AGENTS.md).

---

## 🔧 Tests

```sh
python3 -W error::ResourceWarning -m pytest tests/ -q
```

Los tests usan carpetas temporales y nunca tocan tu home. GitHub Actions los ejecuta en Windows, Ubuntu y macOS.

---

## ⚠️ Limitaciones

- Agydra no instala ni actualiza `agy`, `codex`, `grok` ni `claude`.
- El aislamiento depende de que cada CLI respete su variable de carpeta; `agydra doctor` avisa cuando una CLI cambia su estructura.

## 🤝 Contribuir

Solo librería estándar, el mismo comportamiento en todos los sistemas operativos y la suite de tests en verde. Lee primero [AGENTS.md](AGENTS.md).

## 📄 Licencia

MIT. Consulta [LICENSE](LICENSE).

## 👨‍💻 Autor

**Jaime Andrés Restrepo** — [DragonJAR.org](https://www.dragonjar.org)

- **Organización:** [DragonJAR](https://www.dragonjar.org) — Seguridad, comunidad y herramientas libres
- **Contacto:** contacto@dragonjar.org
- **GitHub:** [@DragonJAR](https://github.com/DragonJAR)

---

*Agydra es un proyecto independiente. No está afiliado a Google, OpenAI, xAI ni Anthropic. `agy` y Antigravity son marcas de Google LLC. Usa cada cuenta según los términos de su proveedor.*
