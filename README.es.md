# Agydra

<p align="center">
  <img src="logo.png" alt="logo de Agydra">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/Licencia-MIT-yellow.svg" alt="Licencia: MIT"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Versi%C3%B3n-1.1.0-blue.svg" alt="Versión"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white" alt="Python"></a>
  <img src="https://img.shields.io/badge/Plataformas-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey.svg" alt="Plataformas">
  <a href="https://www.DragonJAR.org"><img src="https://img.shields.io/badge/author-DragonJAR.org-orange" alt="Author: DragonJAR.org"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/Read%20in-English-0078D4?logo=readme&logoColor=white" alt="Read in English"></a>
</p>

> **Una instalación. Tres motores. Cuentas aisladas.** **Agydra** 1.1.0 es el gestor multi-perfil y despachador de cargas para **Google Antigravity (`agy`)**, **OpenAI Codex (`codex`)** y **xAI Grok (`grok`)**. Cada perfil conserva su almacén, su sesión y su límite de tasa. Las credenciales del host siguen en `~/.gemini`, `~/.codex` y `~/.grok`, y las CLI oficiales se ejecutan tal cual. Python ≥ 3.9, solo librería estándar.

---

## 💡 ¿Por qué Agydra?

Un grupo familiar de Google One AI Premium / Google AI Pro admite hasta seis cuentas, y **cada cuenta tiene sus propias cuotas de modelo y su ventana de 5 horas**. La CLI oficial `agy` sigue leyendo un único `~/.gemini`: cambiar de cuenta pisa la sesión, y las ejecuciones en paralelo comparten el mismo almacén y la misma entrada del llavero.

**Agydra** convierte esas cuentas —y cuentas aparte de Codex o Grok— en un solo pool:

```
agy                 → sesión genérica, ~/.gemini real
agydra -p fam-dev   → el mismo binario agy, con HOME en un overlay aislado
agydra -e grok -r   → el perfil de Grok libre y menos usado
```

- Cada perfil se autentica con la CLI oficial. Los tokens quedan dentro de ese perfil.
- Los locks del kernel (`fcntl.flock` / `msvcrt.locking`) se liberan al salir el proceso, también tras un `SIGKILL`.
- `agydra -r` envía el siguiente comando al perfil libre de ese motor que lleva más tiempo sin usarse.
- `agydra usage` muestra en una sola vista las cuotas en vivo de Antigravity, Codex y Grok.

---

## ⚡ Inicio rápido

### Requisitos

| Requisito | Detalle |
|---|---|
| **Python ≥ 3.9** | Solo librería estándar. |
| **`agy`** *(opcional)* | En el `PATH`, o `-b` / `agy_binary`, para perfiles `agy`. |
| **`codex`** *(opcional)* | En el `PATH`, o `-b` / `AGYDRA_CODEX_BIN`, para perfiles `codex`. |
| **`grok`** *(opcional)* | En el `PATH`, o `-b` / `AGYDRA_GROK_BIN`, para perfiles `grok`. |
| **bwrap** *(opcional)* | Solo Linux, cuando `use_linux_sandbox` está activo. |

### Instalación

```sh
python3 agydra.py          # venv, script de consola y shim en ~/.local/bin
pip install .              # o: pipx install .
agydra doctor
```

### Un perfil por motor

`agy` es el motor por defecto. Codex y Grok repiten los mismos tres pasos con `-e`.

```sh
agydra create trabajo       -d "Cuenta corporativa de Google"
agydra create codex-trabajo -e codex -d "Cuenta corporativa de OpenAI"
agydra create grok-trabajo  -e grok  -d "Cuenta corporativa de xAI"

agydra login trabajo
agydra login codex-trabajo
agydra login grok-trabajo

agydra -p trabajo       "Explica la computación cuántica en tres frases"
agydra -p codex-trabajo "Revisa el último diff de git en busca de problemas de seguridad"
agydra -p grok-trabajo  "Revisa el último diff de git en busca de problemas de seguridad"
```

Lo que va después de los flags del lanzador llega tal cual al motor: `agydra -p grok-trabajo sessions`, `agydra -p codex-trabajo --yolo`.

### Pool familiar y rotación

```sh
agydra create fam-principal -d "Plan familiar - Principal"
agydra create fam-dev       -d "Plan familiar - Código"
agydra create fam-agentes   -d "Plan familiar - Agentes"
agydra login fam-principal && agydra login fam-dev && agydra login fam-agentes

agydra -r "Refactoriza el middleware de autenticación para usar JWT"
agydra -e grok -r "Haz una revisión de seguridad"
agydra usage
```

`agydra list` muestra el correo, el motor y el estado de autenticación. `-r` pide dos perfiles de ese motor: salta el que está ocupado y elige el libre que lleva más tiempo sin usarse. Con `-f` lanza igual cuando todos los candidatos están ocupados.

```text
agydra usage                                     4 perfiles · dom 27 sep · 23:18

■ ANTIGRAVITY                GEMINI                  CLAUDE + GPT

 #   PERFIL    CUENTA         DISPONIBLE SEM · 5H     DISPONIBLE SEM · 5H
 1   neta      agent.bot@g…   ████░ 85    85 ·  94    ██░░░ 46    46 · 100
 2   vacan     deep.res@gm…   ████░ 73    73 ·  99    █████ 100  100 · 100

■ OPENAI CODEX

 #   PERFIL    CUENTA         DISPONIBLE SEM · 5H     ↻        PLAN
 3   codex     lead.dev@gm…   █████ 99    99 · 100    5d 5h    ChatGPT Plus

■ XAI GROK

 #   PERFIL    CUENTA         DISPONIBLE ↻        PLAN
 4   grok-work corp.ops@xa…   ████░ 82    6d 2h    SuperGrok

▸ USAR AHORA   Gemini → neta 85%   Claude/GPT → vacan 100%   Codex → codex 99%   Grok → grok-work 82%
```

Las columnas se comprimen en una terminal estrecha. `agydra usage vacan` muestra la cuenta regresiva de un perfil. `usage` es de solo lectura y puede correr al lado de una sesión activa.

---

## 🚀 Flujos principales

**Fijar un directorio.** `agydra use cliente-acme` escribe `.agydra`. Los comandos siguientes en ese árbol usan ese perfil, y el marcador gana sobre `-r`.

**Rotar.** `agydra -r` se queda en `agy`. `agydra -e codex -r` y `agydra -e grok -r` rotan dentro de ese motor.

**Compartir configuración y dejar las credenciales.** `agydra share-config trabajo personal pruebas` copia `settings.json`, `mcp.json` y `config.toml`. `auth.json` se queda en el perfil de origen.

**Simulación.** `agydra -np trabajo` imprime rutas, entorno y argv, y no lanza nada.

**Idioma.** `agydra lang es` guarda `en` o `es` en `agydra.json`. `AGYDRA_LANG=es` vale para un solo proceso. `agydra lang` muestra el idioma activo y los códigos disponibles.

### Qué aísla cada motor

| Motor | Aislamiento | Login | Notas |
|---|---|---|---|
| `agy` | Overlay de `HOME`, diseño de `~/.gemini` | OAuth oficial de Google | Puente de llavero en macOS (`agydra.<perfil>`). El `~/.gemini` del host queda como estaba. |
| `codex` | `CODEX_HOME` → `~/.codex` del overlay | `codex login` | `--no-daemon` en cada ejecución, y `daemon_auto_start = false`, para que la ruta del socket quede bajo `SUN_LEN` y el lock se suelte al salir. Las credenciales viven en `auth.json`. |
| `grok` | `GROK_HOME` y `GROK_LEADER_SOCKET` (`<overlay>/.grok/leader.sock`) | `grok login` | El `~/.grok` del host queda como estaba. Planes como SuperGrok salen de `auth.json`. `usage` lee la API de facturación de xAI. Sin intercambio de llavero. |

---

## 🧭 Comandos y flags

Los comandos de gestión también aceptan `--NOMBRE` o `-NOMBRE` (`agydra --list` == `agydra list`). Un perfil es un nombre o el índice en base 1 de `agydra list`.

| Comando | Alias | Descripción |
|---|---|---|
| `list` | `ls`, `l` | Índice, correo, default, auth, ocupado, motor, último uso. |
| `create NOMBRE [-d DESC] [-e MOTOR]` | `c` | Almacén nuevo. `-e` es `agy` (por defecto), `codex` o `grok`. |
| `login [NOMBRE\|#] [-f] [-n]` | `in` | Login aislado del motor de ese perfil. `-f` vuelve a autenticar. |
| `import NOMBRE\|# [-s DIR]` | `imp` | **Copia** `~/.gemini`, `~/.codex` o `~/.grok` al perfil mientras mantiene el lock de ese perfil. |
| `rename A B` | `mv` | Renombra el perfil y actualiza el default. Rechaza un perfil ocupado. |
| `delete NOMBRE\|# [-f] [--no-backup]` | `rm` | Escribe un ZIP en `backups/` y luego borra. Rechaza un perfil ocupado. |
| `default [NOMBRE\|#]` | `d` | Muestra o fija el perfil de respaldo. |
| `use [NOMBRE\|#]` | `u` | Fija el directorio actual con un marcador `.agydra`. |
| `status [-n]` | `st` | Perfil resuelto, motor, binario, correo, auth y lock. |
| `usage [NOMBRE\|#]` | `us` | Cuotas en vivo de `agy`, `codex` y `grok`. |
| `share-config ORIGEN DESTINO...` | `share` | Copia archivos de configuración mientras mantiene los locks de los perfiles de origen y destino hasta terminar todas las copias. Las credenciales se quedan en el origen. |
| `doctor [--fix] [-f]` | `doc` | Diez chequeos. `--fix` repara los enlaces de datos del overlay y los defaults colgantes, y purga solo artefactos huérfanos. |
| `setup [-n]` | `install` | Venv, script de consola y shim en el PATH, de forma idempotente. |
| `lang [CÓDIGO]` | `language`, `idioma`, `locale` | Idioma de la interfaz: `en` o `es`. |
| `version` | `-v`, `--version` | Agydra, Python y el sistema. |
| `help [COMANDO]` | `-h`, `--help` | Ayuda de Agydra o de un subcomando. |

Los flags del lanzador van **antes** de los argumentos del motor.

| Flag | Forma larga | Descripción |
|:---:|---|---|
| `-p NOMBRE\|#` | `--profile` | Perfil por nombre o índice. |
| `-r` | `--random` | Perfil autenticado, libre y menos usado. `-e` limita el pool. |
| `-e MOTOR` | `--engine` | `agy` (por defecto), `codex` o `grok`. |
| `--lang CÓDIGO` | — | Fija y persiste `en` o `es`. |
| `-n` | `--dry-run` | Imprime el plan. No lanza. |
| `-b RUTA` | `--binary` | Sustituye el binario del motor en esta ejecución. |
| `-f` | `--force` | Omite el lock de sesión. |

Los flags cortos se agrupan (`-nr` == `-n -r`). `-p` junto con `-r` sale con código `2`.

| Combinación | Para qué |
|---|---|
| `agydra -p grok-trabajo "prompt"` | Lanza Grok con ese perfil. |
| `agydra -e grok -r "prompt"` | Rota entre perfiles de Grok libres. |
| `agydra -e codex -r "prompt"` | Rota entre perfiles de Codex libres. |
| `agydra -rf "prompt"` | Rota, y lanza igual si todos los perfiles están ocupados. |
| `agydra -np trabajo` | Muestra el plan de lanzamiento de `trabajo`. |
| `agydra -b /ruta/grok -p grok-trabajo` | Prueba un binario concreto con las credenciales de ese perfil. |

Sin `-p`, gana la primera coincidencia:

```
1. --profile / -p
   └── 2. AGYDRA_PROFILE
       └── 3. Marcador .agydra (ancestro más cercano; anula -r)
           └── 4. default_profile en agydra.json
               └── 5. Primer perfil en orden alfabético
```

**Códigos de salida:** `0` éxito · `1` error · `2` `-p` con `-r` · `126` binario no ejecutable · `127` binario no encontrado · `130` Ctrl-C.

---

## 🛡️ Arquitectura

```
Home del host (~/)                     en su sitio: ~/.gemini  ~/.codex  ~/.grok

<store>/profiles/<nombre>/data         tokens y configuración reales
<store>/overlays/<nombre>              lo que ve el proceso hijo
    agy    HOME → overlay,  .gemini → profiles/<nombre>/data
    codex  CODEX_HOME → overlay/.codex
    grok   GROK_HOME  → overlay/.grok
           GROK_LEADER_SOCKET → overlay/.grok/leader.sock
```

`.ssh`, `.gitconfig` y la configuración del shell se reflejan en el overlay. Los directorios ancestros del almacén son directorios reales, así que el almacén queda fuera del alcance desde dentro del overlay. El hijo recibe `AGYDRA_REAL_HOME`.

`AgyEngine`, `CodexEngine` y `GrokEngine` se encargan de encontrar el binario, los argumentos, las rutas y la identidad. Codex siempre corre con `--no-daemon`. El socket leader de Grok es por perfil, así que una sesión `grok` del host y una de perfil no lo comparten. El puente de llavero de macOS aplica solo a `agy`.

Los locks de sesión usan `<store>/locks/<perfil>.lock`; el archivo marcador persiste, mientras el lock advisory del sistema operativo se libera cuando termina el proceso que lo posee. `create`, `rename` y `delete` adquieren los locks de los perfiles afectados en orden ascendente por nombre y luego el lock persistente, compartido y no heredado `<store>/locks/.profile-sequence.lock`; los locks de perfil y de secuencia se adquieren sin espera, y los locks de perfil se liberan en orden inverso. El archivo persistente `<store>/profile-sequence.json` guarda `{"last_seq": N}`. Si falta, el contador se inicializa con la secuencia más alta de los perfiles con metadatos legibles mientras se mantiene el lock de secuencia. `create` guarda el número siguiente antes de preparar el perfil; una interrupción o un fallo posterior puede dejar un salto inocuo, y no se reutilizan secuencias de perfiles eliminados.

`create` prepara `data/` y `profile.json` en un directorio temporal hermano, del mismo sistema de archivos, dentro de `<store>/profiles/`, con el nombre `.agydra-stage-<nombre>-<token>`, y publica el perfil completo con un único cambio de nombre de directorio. Los listados ignoran una etapa que quedó tras una interrupción; un `create` posterior para ese mismo nombre la elimina mientras mantiene ambos locks. El número de secuencia reservado sigue consumido.

Antes de mover el directorio de un perfil, `rename` escribe atómicamente su intención en `<store>/profile-rename.json`, que incluye una acción de recuperación para migrar el slot de Keychain y la instantánea `source_present`. Una lectura pública o mutación posterior de perfiles recupera ese journal mientras mantiene, en orden ascendente por nombre, los locks de los perfiles anterior y nuevo, seguidos por el lock de secuencia. Si solo existe el directorio anterior, la recuperación restaura el default anterior cuando hace falta y elimina el journal; si solo existe el nuevo, completa el cambio: corrige los metadatos y el default cuando hace falta, elimina el overlay anterior y luego ejecuta la acción de Keychain registrada antes de borrar el journal. La recuperación forward ejecuta esa acción mientras mantiene los locks de perfil ordenados, el lock de secuencia y `swap.lock`; si falla, conserva el journal para reintentar en la siguiente operación del almacén. En el camino normal de rename, el callback se ejecuta con los locks de perfil mantenidos, después de liberar el lock de secuencia. Si existen ambos directorios o ninguno, el journal o los metadatos están dañados, o algún lock requerido está ocupado, la recuperación falla de forma segura y conserva el journal y los datos de perfil.

Cuando está activado (por defecto; `--no-backup` lo desactiva), `delete` crea y verifica el ZIP de respaldo antes de borrar los datos del perfil. La CLI conserva el lock de perfil durante la purga de llavero posterior al borrado, después de liberar el lock de secuencia; en macOS, la purga usa `swap.lock` para serializar la limpieza del slot guardado y de la entrada del Llavero del sistema con los cambios de credenciales. Si falla la purga, se informa una advertencia después del borrado. `import` mantiene el lock del perfil de destino durante la copia y `share-config` mantiene los locks de los perfiles de origen y destino hasta terminar todas las copias. `doctor --fix` mantiene el lock del perfil mientras migra y vuelve a enlazar los datos del overlay.

| Plataforma | Mecanismo |
|---|---|
| **macOS** | Puente de llavero para `agy`. Codex y Grok lo omiten. |
| **Linux** | `bwrap` opcional (`use_linux_sandbox`) enmascara los sockets de DBus y del keyring. Si falta `bwrap`, avisa y sigue. |
| **Windows** | Junctions NTFS. Sin permisos de administrador y sin modo de desarrollador. |

---

## 🩺 Diagnóstico

`agydra doctor` hace diez chequeos: binarios que de verdad se usan, almacén, perfiles, locks, aislamiento, llavero, canario de esquema, huérfanos, sandbox de Linux y el shim del PATH. `agydra doctor --fix` migra al almacén del perfil un directorio real heredado que ocupa la ruta de datos del overlay de un perfil existente y vuelve a enlazar esa ruta mientras mantiene el lock del perfil; borra el default configurado cuando su nombre no aparece en la lista actual de perfiles con metadatos legibles. Para limpiar huérfanos, el directorio del perfil cuenta como propietario aunque no se puedan leer sus metadatos. Doctor elimina directorios de overlay huérfanos, copias de seguridad de secretos y archivos en cuarentena del llavero, y archivos ZIP de respaldo solo cuando falta el directorio del perfil propietario; intenta adquirir el lock de cada perfil candidato y omite la limpieza si una sesión activa lo mantiene ocupado. Los slots del llavero del sistema también se purgan solo cuando falta el directorio del perfil, tras adquirir primero el lock del perfil y luego `swap.lock`. Los archivos del lock de sesión persisten como marcadores y no se eliminan.

---

## ⚙️ Configuración

La configuración global está en `<store>/agydra.json`. Otro almacén se indica con `AGYDRA_HOME`.

| Sistema | Almacén por defecto |
|---|---|
| **macOS** | `~/Library/Application Support/agydra` |
| **Linux** | `$XDG_DATA_HOME/agydra` o `~/.local/share/agydra` |
| **Windows** | `%LOCALAPPDATA%\agydra` |

```json
{
  "default_profile": "trabajo",
  "settings": {
    "use_linux_sandbox": false,
    "copy_settings_on_create": true,
    "windows_redirect_home": false
  },
  "agy_binary": "/usr/local/bin/agy",
  "codex_binary": "/usr/local/bin/codex",
  "grok_binary": "/usr/local/bin/grok"
}
```

`codex_binary` y `grok_binary` son opcionales. Si omites una clave, Agydra usa el `PATH`.

| Variable | Para qué |
|---|---|
| `AGYDRA_PROFILE` | Perfil de respaldo. Ganan `-p` y `.agydra`. |
| `AGYDRA_LANG` | `en` o `es` para un proceso. |
| `AGYDRA_HOME` | Almacén y overlays. |
| `AGYDRA_AGY_BIN` / `AGYDRA_CODEX_BIN` / `AGYDRA_GROK_BIN` | Binario alternativo. |
| `AGYDRA_NO_KEYCHAIN` | Omite el puente de llavero de macOS. |
| `NO_COLOR` / `FORCE_COLOR` | Estilo ANSI. |

---

## 🔧 Pruebas

```sh
python3 -W error::ResourceWarning -m pytest tests/ -q
python3 -m unittest discover -s tests -q
```

La suite (650+ pruebas) usa almacenes temporales y deja el home del host en paz.

---

## ⚠️ Limitaciones

- Agydra gestiona perfiles y sesiones. No instala ni actualiza `agy`, `codex` ni `grok`.
- El aislamiento sigue a cada CLI: `HOME` para `agy`, `CODEX_HOME` para `codex`, `GROK_HOME` más `GROK_LEADER_SOCKET` para `grok`. El canario de esquema de Doctor avisa si el diseño no coincide con los drivers actuales.
- Las rutas de Windows se ejercitan en Windows. En Unix las cubren las pruebas unitarias.

---

## 🤝 Contribuir

1. Solo librería estándar en runtime.
2. El mismo comportamiento en macOS, Linux y Windows.
3. Corre la suite antes de un pull request.
4. Lee [AGENTS.md](AGENTS.md) para los invariantes.

---

## 📄 Licencia

MIT. Ver [LICENSE](LICENSE).

## 👨‍💻 Autor

**Jaime Andrés Restrepo** — [DragonJAR.org](https://www.dragonjar.org)

- **Organización:** [DragonJAR](https://www.dragonjar.org) — Seguridad, comunidad y herramientas libres
- **Contacto:** contacto@dragonjar.org
- **GitHub:** [@DragonJAR](https://github.com/DragonJAR)

---

*Agydra es un proyecto independiente. No está afiliado a Google, OpenAI ni xAI. `agy` y Antigravity son marcas de Google LLC. Usa cada cuenta según los términos de su proveedor.*
