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

> **Una instalación. Cuatro motores. Cuentas aisladas.** **Agydra** 1.1.0 es el gestor multi-perfil y despachador de cargas para **Google Antigravity (`agy`)**, **OpenAI Codex (`codex`)**, **xAI Grok (`grok`)** y **Anthropic Claude Code (`claude`)**. Cada perfil conserva su configuración y autenticación nativa; los snapshots de Claude Code son informativos. Claude Code conserva el HOME real y gestiona su autenticación nativa, incluido el llavero de macOS; Agydra no extrae, intercambia ni refresca sus tokens por cuenta propia. Python ≥ 3.9, solo librería estándar.

---

## 💡 ¿Por qué Agydra?

Un grupo familiar de Google One AI Premium / Google AI Pro admite hasta seis cuentas, y **cada cuenta tiene sus propias cuotas de modelo y su ventana de 5 horas**. La CLI oficial `agy` sigue leyendo un único `~/.gemini`: cambiar de cuenta pisa la sesión, y las ejecuciones en paralelo comparten el mismo almacén y la misma entrada del llavero.

**Agydra** convierte esas cuentas —y cuentas aparte de Codex, Grok o Claude Code— en un solo pool:

```
agy                 → sesión genérica, ~/.gemini real
agydra -p fam-dev   → el mismo binario agy, con HOME en un overlay aislado
agydra -e grok -r   → el siguiente perfil de Grok sin usar, priorizado por cuota guardada
```

- Cada perfil se autentica con la CLI oficial. Los tokens quedan dentro de ese perfil.
- Los locks del kernel (`fcntl.flock` / `msvcrt.locking`) se liberan al salir el proceso, también tras un `SIGKILL`.
- `agydra -r` usa cada perfil elegible una vez por ciclo del ámbito de selección. Sin `-e`, el grupo incluye perfiles autenticados de todos los motores; `-e` lo limita a un motor. Primero elige perfiles sin usar, priorizando una sesión libre y luego más cuota guardada. Cuando se agota el grupo, las repeticiones priorizan la cuota guardada. La cuota desconocida queda detrás de la conocida; actualízala con `agydra usage`.
- `agydra usage` muestra en una sola vista las cuotas en vivo y el estado en Antigravity, Codex, Grok y Claude Code.

---

## ⚡ Inicio rápido

### Requisitos

| Requisito | Detalle |
|---|---|
| **Python ≥ 3.9** | Solo librería estándar. |
| **`agy`** *(opcional)* | En `PATH`, o `-b` / `AGYDRA_AGY_BIN`, para perfiles `agy`. |
| **`codex`** *(opcional)* | En el `PATH`, o `-b` / `AGYDRA_CODEX_BIN`, para perfiles `codex`. |
| **`grok`** *(opcional)* | En el `PATH`, o `-b` / `AGYDRA_GROK_BIN`, para perfiles `grok`. |
| **`claude`** *(opcional)* | En `PATH`, o `-b` / `AGYDRA_CLAUDE_BIN`, para perfiles Claude Code. |
| **bwrap** *(opcional)* | Solo Linux, cuando `use_linux_sandbox` está activo. |

### Instalación

```sh
python3 agydra.py          # venv, script de consola y shim en ~/.local/bin
pip install .              # o: pipx install .
agydra doctor
```

`python3 agydra.py` prepara un checkout de fuentes y re-ejecuta el comando instalado; tras una instalación con `pip`/`pipx` (`python -m agydra` entra directo a la CLI), `agydra setup` detecta la instalación propiedad de pip, lo informa y no escribe nada — actualízala o elimínala con pip/pipx.

### Un perfil por motor

`agy` es el motor por defecto. Codex, Grok y Claude Code repiten los mismos tres pasos con `-e`.

```sh
agydra create trabajo       -d "Cuenta corporativa de Google"
agydra create codex-trabajo -e codex -d "Cuenta corporativa de OpenAI"
agydra create grok-trabajo  -e grok  -d "Cuenta corporativa de xAI"

agydra login trabajo
agydra login codex-trabajo
agydra login grok-trabajo
agydra create claude-trabajo -e claude
agydra login claude-trabajo
agydra -p claude-trabajo "Revisa este proyecto"

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

`agydra list` muestra el correo, el motor y el estado de autenticación. `-r` conserva un ciclo por ámbito de selección: todos los motores si se omite `-e`, o el motor indicado si se especifica. Selecciona cada perfil elegible antes de repetir; cuando todos se usaron, la siguiente ejecución inicia otro ciclo. Entre perfiles sin usar, prioriza una sesión libre y luego la mayor cuota restante de un snapshot de no más de 15 minutos. Al repetir, prioriza la cuota, después la prioridad de sesión y el menor uso reciente. Las lecturas ausentes, obsoletas, vencidas o inválidas quedan al final. Ejecuta `agydra usage` para actualizar las cuotas. `.agydra` fija la resolución normal del perfil, mientras que `-r` lo ignora. Las sesiones simultáneas son ilimitadas por defecto; define `settings.max_sessions_per_profile` en `agydra.json` para limitarlas. `-f` omite ese límite sin cambiar el orden de rotación.

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

### Perfiles Claude Code y captura de uso opt-in

```sh
agydra create claude-trabajo -e claude
agydra login claude-trabajo
agydra status claude-trabajo
agydra -p claude-trabajo "Revisa este proyecto"
agydra usage claude-trabajo
agydra usage --claude-settings claude-trabajo
```

Instala Claude Code por separado. El binario se busca mediante `-b`, `AGYDRA_CLAUDE_BIN`, `claude_binary` y después `PATH`. Login ejecuta `claude auth login` nativo; status/list consultan `claude auth status` con tiempo limitado, y las respuestas no reconocidas quedan desconocidas. El entorno del perfil elimina credenciales OAuth, claves API y overrides de proveedor heredados del proceso padre. Login puede requerir el flujo interactivo del navegador nativo; esta integración no verifica proveedores cloud ni flujos Console sin clave.

`agydra status claude-trabajo` imprime la ruta física de configuración y el `seq` inmutable. `CLAUDE_CONFIG_DIR` apunta a `<store>/claude-config/<seq>` y conserva el HOME real. Rename conserva ruta e identidad nativa. Claude Code gestiona sus credenciales, incluido el llavero de macOS; Agydra no usa el puente de Antigravity ni afirma portabilidad OAuth/Keychain. Delete respalda la configuración física y elimina la caché del perfil después del respaldo; el respaldo puede incluir credenciales guardadas en disco, pero no exporta entradas del llavero nativo, que requieren re-login nativo. Los perfiles Claude rechazan `import` y `share-config` hasta disponer de copias selectivas seguras. Configúralos explícitamente y autentícate mediante la CLI nativa.

El uso de Claude Code se muestra en **ANTHROPIC CLAUDE CODE**, separado de las cuotas Claude/GPT de Antigravity, desde dos fuentes en este orden. (1) **En vivo**: `agydra usage` ejecuta el propio `claude -p "/usage" --no-session-persistence --safe-mode` del perfil (`CLAUDE_CONFIG_DIR` aislado, `TZ=UTC`, unos 2 s) y lee los porcentajes de sesión y semana y sus horas de reinicio; el resultado se guarda 5 minutos y se marca `en vivo (confirmado por el servidor)`. El `claude` del perfil gestiona su login y la renovación del token, así que Agydra nunca lee, refresca ni escribe credenciales, no toca el llavero y funciona igual en macOS, Linux y Windows; `--safe-mode` evita además que se ejecuten los hooks, plugins y servidores MCP del perfil. Un binario ausente, un timeout, una salida sin uso de suscripción (sin login o login por clave API) o un reinicio ilegible solo hacen volver a (2). (2) **Snapshot de statusLine**: snapshot local informativo en el que `statusLine` no identifica la cuenta; su fecha es la observación local, no la de una consulta al servidor. Solo aparecen las ventanas observadas: un snapshot parcial no permite inferir disponibilidad total, y un reinicio vencido queda desconocido en lugar de 100%. Los snapshots recientes se marcan observados (en caché), los antiguos obsoletos, las sesiones contradictorias ambiguas y los datos ausentes/inválidos desconocidos. En la tabla compacta de `usage`, Claude Code usa las mismas columnas que los demás motores (`ACCOUNT`, `AVAILABLE`, `WK · 5H`, `↻`) más una columna `ESTADO` (`en vivo`, `snapshot`, `obsoleto`, `ambiguo`, `desconocido`); la fuente y las salvedades quedan en `agydra usage <perfil>`. Solo las lecturas `en vivo` confirmadas por el servidor alimentan la línea `USE NOW`, nunca los snapshots de statusLine.

La captura está deshabilitada por defecto. `agydra usage --claude-settings claude-trabajo` imprime un fragmento JSON con un comando `statusLine` independiente. Usa el intérprete de Agydra en ejecución (`sys.executable`), `python -m claude_usage --store <store> --seq <seq> --display` y quoting POSIX en macOS/Linux y un comando PowerShell literal codificado en Windows, incluidas rutas con espacios. En Windows, la captura requiere PowerShell. Ese intérprete debe tener instalado el módulo empaquetado `claude_usage`; regenera el fragmento si cambia la instalación para actualizar su ruta.

Copia o combina el objeto `statusLine` impreso **manualmente en `settings.json` dentro de la ruta física del perfil mostrada por status**. Por defecto Agydra no escribe ese archivo; `--claude-settings PERFIL --apply` lo escribe por ti: crea `settings.json` si no existe, o combina de forma atómica el `statusLine` de Agydra en uno existente que no tenga `statusLine`, conservando todas las demás claves. No hace nada si el `statusLine` de Agydra ya está presente, y se niega, dejando el archivo intacto, si hay un `statusLine` distinto o JSON inválido. El comando independiente muestra una línea breve y guarda únicamente ventanas de cuota permitidas; no conserva automáticamente un statusLine previo. Si ya tienes uno, consérvalo hasta componer explícitamente ambos comandos; no se proporciona un wrapper automático. `--settings`, políticas administradas o statusLine deshabilitado pueden impedir la captura; Agydra no fuerza overrides. Lanza mediante Agydra para que el escritor reciba la secuencia del perfil y su generación de login actual. La generación de uso solo se invalida con `auth login`/`auth logout` nativos ejecutados mediante Agydra y con delete; los payloads de una generación anterior ya no pueden repoblar la caché. Un `/login` interactivo dentro de una sesión de Claude en ejecución no es detectable, por lo que el snapshot sigue siendo informativo y la identidad de la cuenta sin verificar.

---

## 🚀 Flujos principales

**Fijar un directorio.** `agydra use cliente-acme` escribe `.agydra`. La resolución normal del perfil en ese árbol usa el perfil fijado; un `-r` explícito ignora el marcador y rota dentro del ciclo de su ámbito de selección.

**Rotar.** `agydra -r` considera perfiles autenticados de todos los motores. Añade `-e codex`, `-e grok`, `-e claude` o `-e agy` para limitar el grupo. Cada ámbito agota los perfiles sin usar antes de repetir: los no usados priorizan una sesión libre y luego la cuota guardada; las repeticiones priorizan la cuota guardada, después la prioridad de sesión y el menor uso reciente. Las sesiones `agy` elegidas por rotación usan la autenticación nativa en disco del perfil seleccionado: distintas cuentas pueden coexistir sin compartir el slot del llavero de macOS ni sustituir la selección por su dueño. El ciclo identifica perfiles, no correos; dos perfiles de la misma cuenta siguen siendo entradas independientes.

`agydra -e agy --force -r --dangerously-skip-permissions` conserva ese orden y omite únicamente el límite de sesiones. Agydra establece `SSH_TTY=agydra-profile` para elegir autenticación nativa en archivo y mantiene la terminal interactiva. Las ejecuciones anidadas no heredan el marcador (solo el lanzamiento que selecciona la autenticación en archivo lo establece). Los tokens renovados quedan en el perfil seleccionado. Un token ausente se publica atómicamente desde un respaldo privado con identidad verificada, solo mientras el perfil está libre; archivos malformados, ajenos o inseguros abortan sin heredar otra cuenta. Un perfil funciona con `-r` justo después de `agydra login PERFIL`: el login registra el correo de la cuenta, y un perfil sin correo registrado confía en su primera identidad detectada y la registra, mientras que uno cuya credencial contradice su correo registrado sigue rechazándose. Autentica un perfil no preparado con `agydra login PERFIL`. `-p PERFIL` explícito y login conservan el puente del llavero.

**Compartir configuración y dejar las credenciales.** `agydra share-config trabajo personal pruebas` copia `settings.json`, `mcp.json` y `config.toml`. `auth.json` se queda en el perfil de origen.

**Simulación.** `agydra -np trabajo` imprime rutas, entorno y argv, y no lanza nada.

**Idioma.** `agydra lang es` guarda `en` o `es` en `agydra.json`. `AGYDRA_LANG=es` vale para un solo proceso. `agydra lang` muestra el idioma activo y los códigos disponibles.

### Qué aísla cada motor

| Motor | Aislamiento | Login | Notas |
|---|---|---|---|
| `agy` | Overlay de `HOME`, diseño de `~/.gemini` | OAuth oficial de Google | Rotación usa tokens privados en disco; selección explícita/login conservan el puente del llavero. El `~/.gemini` del host queda como estaba. |
| `codex` | `CODEX_HOME` → `~/.codex` del overlay | `codex login` | `--no-daemon` en cada ejecución, y `daemon_auto_start = false`, para que la ruta del socket quede bajo `SUN_LEN` y el lock se suelte al salir. Las credenciales viven en `auth.json`. |
| `grok` | `GROK_HOME` y `GROK_LEADER_SOCKET` (`<overlay>/.grok/leader.sock`) | `grok login` | El `~/.grok` del host queda como estaba. Planes como SuperGrok salen de `auth.json`. `usage` lee la API de facturación de xAI. Sin intercambio de llavero. |
| `claude` | `CLAUDE_CONFIG_DIR` → `<store>/claude-config/<seq>` físico y estable; HOME real | `claude auth login` | Autenticación comprobada con `claude auth status`; sin intercambio del llavero de Antigravity. Rename conserva ruta e identidad. Usage ejecuta el `claude -p "/usage"` del propio perfil (en caché 5 min); si no, snapshots statusLine opt-in. |

---

## 🧭 Comandos y flags

Los comandos de gestión también aceptan `--NOMBRE` o `-NOMBRE` (`agydra --list` == `agydra list`). Un perfil es un nombre o el índice en base 1 de `agydra list`.

| Comando | Alias | Descripción |
|---|---|---|
| `list` | `ls`, `l` | Índice, correo, default, auth, ocupado, motor, último uso. |
| `create NOMBRE [-d DESC] [-e MOTOR]` | `c` | Almacén nuevo. `-e` es `agy` (por defecto), `codex`, `grok` o `claude`. |
| `login [NOMBRE\|#] [-f] [-n]` | `in` | Login aislado del motor de ese perfil. `-f` vuelve a autenticar. |
| `import NOMBRE\|# [-s DIR]` | `imp` | **Copia** `~/.gemini`, `~/.codex` o `~/.grok` al perfil mientras mantiene el lock de ese perfil. Los perfiles Claude se rechazan. |
| `export NOMBRE\|# [-o RUTA]` | `exp` | **Escribe** un ZIP portable del perfil (excluye secretos OAuth/API key/Keychain por la política R4; Claude se rechaza). Destino por defecto: `~/agydra-export-<nombre>-<ts>.zip`. |
| `rename A B` | `mv` | Renombra el perfil y actualiza el default. Rechaza un perfil ocupado. |
| `delete NOMBRE\|# [-f] [--no-backup]` | `rm` | Escribe un ZIP en `backups/` y luego borra. Rechaza un perfil ocupado. |
| `default [NOMBRE\|#]` | `d` | Muestra o fija el perfil de respaldo. |
| `use NOMBRE\|#` | `u` | Fija el directorio actual con un marcador `.agydra`. |
| `status [NOMBRE\|#] [-p NOMBRE\|#] [-e MOTOR] [-n]` | `st` | Perfil resuelto, motor, binario (`not found` si el CLI del motor no está instalado), correo, auth y sesiones activas. `-n` imprime el plan de lanzamiento y sí necesita el binario. |
| `usage [NOMBRE\|#]` | `us` | Cuotas de `agy`, `codex`, `grok` y Claude Code (consultas en vivo y snapshots informativos). `--claude-settings PERFIL` imprime ajustes opt-in; `--apply` (solo con `--claude-settings`) escribe el `statusLine` en el `settings.json` del perfil (lo crea o lo combina de forma atómica conservando las demás claves); no hace nada si ya está presente; se niega y deja el archivo intacto si hay un `statusLine` distinto o JSON inválido. |
| `share-config ORIGEN DESTINO...` | `share` | Copia archivos de configuración mientras mantiene los locks de los perfiles de origen y destino hasta terminar todas las copias. Las credenciales se quedan en el origen. Los perfiles Claude se rechazan. |
| `doctor [--fix [-f]]` | `doc` | Diez chequeos. `--fix` repara los enlaces de datos del overlay y los defaults colgantes, y purga solo artefactos huérfanos; `-f` omite la confirmación de `--fix` y se rechaza sin `--fix`. |
| `setup [-n] [-f]` | `install` | Venv, script de consola y shim en el PATH, de forma idempotente. `-n` previsualiza; `-f` sobrescribe un shim ajeno. |
| `language [CÓDIGO]` | `lang`, `idioma`, `locale` | Idioma de la interfaz: `en` o `es`. |
| `version` | `--version` | Imprime la versión y el autor de Agydra. |
| `help` | `h`, `-h`, `--help` | Ayuda general. `agydra COMANDO --help` muestra un subcomando. |

Los flags del lanzador van **antes** de los argumentos del motor.

| Flag | Forma larga | Descripción |
|:---:|---|---|
| `-p NOMBRE\|#` | `--profile` | Perfil por nombre o índice. |
| `-r` | `--random` (`--rotate`) | Usa cada perfil elegible una vez por ciclo del ámbito antes de repetir. Sin `-e`, incluye perfiles autenticados de todos los motores; con `-e`, solo ese motor. Los no usados priorizan sesión libre y luego cuota guardada; las repeticiones priorizan cuota. Ignora `.agydra`. |
| `-e MOTOR` | `--engine` | `agy` (motor normal por defecto), `codex`, `grok` o `claude`. Con `-r`, limita el grupo a ese motor. |
| `--lang CÓDIGO` | — | Fija y persiste `en` o `es`. |
| `-n` | `--dry-run` | Imprime el plan. No lanza. |
| `-b RUTA` | `--binary` | Sustituye el binario del motor en esta ejecución. |
| `-f` | `--force` | Ignora el límite opcional `max_sessions_per_profile`. Unirse a un perfil ocupado ya es el comportamiento por defecto. |

Los flags cortos se agrupan (`-nr` == `-n -r`). `-p` junto con `-r` sale con código `2`.

| Combinación | Para qué |
|---|---|
| `agydra -p grok-trabajo "prompt"` | Lanza Grok con ese perfil. |
| `agydra -r "prompt"` | Rota entre perfiles autenticados de todos los motores; elige primero perfiles sin usar. |
| `agydra -e grok -r "prompt"` | Rota entre perfiles elegibles de Grok; agota los nuevos antes de repetir según cuota. |
| `agydra -e codex -r "prompt"` | Rota entre perfiles elegibles de Codex; agota los nuevos antes de repetir según cuota. |
| `agydra -rf "prompt"` | Rota entre perfiles de todos los motores, omitiendo el límite opcional `max_sessions_per_profile`. |
| `agydra -np trabajo` | Muestra el plan de lanzamiento de `trabajo`. |
| `agydra -b /ruta/grok -p grok-trabajo` | Prueba un binario concreto con las credenciales de ese perfil. |

Sin `-p`, gana la primera coincidencia:

```
1. --profile / -p
   └── 2. AGYDRA_PROFILE
       └── 3. Marcador .agydra (ancestro más cercano; ignorado por -r)
           └── 4. default_profile en agydra.json
               └── 5. Primer perfil por número (orden de creación; primero los de `agy`)
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
    claude CLAUDE_CONFIG_DIR → <store>/claude-config/<seq> (física; conserva HOME)
```

Para los motores con overlay, `.ssh`, `.gitconfig` y la configuración del shell se reflejan en el overlay. Los directorios ancestros del almacén son directorios reales, así que el almacén queda fuera del alcance desde dentro del overlay. El hijo recibe `AGYDRA_REAL_HOME`.

`AgyEngine`, `CodexEngine`, `GrokEngine` y `ClaudeEngine` se encargan de encontrar el binario, los argumentos, las rutas y la identidad. Codex siempre corre con `--no-daemon`. El socket leader de Grok es por perfil, así que una sesión `grok` del host y una de perfil no lo comparten. El puente del llavero de macOS aplica solo a selección explícita/login de `agy`. Las sesiones con autenticación en archivo cuentan para los límites y protegen su perfil de mutaciones, pero no prolongan la propiedad del slot compartido.

Los locks de sesión usan `<store>/locks/<perfil>.lock`; el archivo marcador persiste, mientras el lock advisory del sistema operativo se libera cuando termina el proceso que lo posee. Las sesiones simultáneas se unen registrándose en el registro de leases de poseedores del archivo bloqueado (`acquire_lease`/`release_lease`), que rastrea el PID y la vigencia del token de inicio del proceso distinguiendo a los participantes del llavero de los poseedores con autenticación privada. `create`, `rename` y `delete` adquieren los locks de los perfiles afectados en orden ascendente por nombre y luego el lock persistente, compartido y no heredado `<store>/locks/.profile-sequence.lock`; los locks de perfil y de secuencia se adquieren sin espera, y los locks de perfil se liberan en orden inverso. El archivo persistente `<store>/profile-sequence.json` guarda `{"last_seq": N}`. Si falta, el contador se inicializa con la secuencia más alta de los perfiles con metadatos legibles mientras se mantiene el lock de secuencia. `create` guarda el número siguiente antes de preparar el perfil; una interrupción o un fallo posterior puede dejar un salto inocuo, y no se reutilizan secuencias de perfiles eliminados.

`create` prepara `data/` y `profile.json` en un directorio temporal hermano, del mismo sistema de archivos, dentro de `<store>/profiles/`, con el nombre `.agydra-stage-<nombre>-<token>`, y publica el perfil completo con un único cambio de nombre de directorio. Los listados ignoran una etapa que quedó tras una interrupción; un `create` posterior para ese mismo nombre la elimina mientras mantiene ambos locks. El número de secuencia reservado sigue consumido.

Antes de mover el directorio de un perfil, `rename` escribe atómicamente su intención en `<store>/profile-rename.json`, que incluye una acción de recuperación para migrar el slot de Keychain y la instantánea `source_present` para Antigravity; el rename de Claude no añade esta acción. Una lectura pública o mutación posterior de perfiles recupera ese journal mientras mantiene, en orden ascendente por nombre, los locks de los perfiles anterior y nuevo, seguidos por el lock de secuencia. Si solo existe el directorio anterior, la recuperación restaura el default anterior cuando hace falta y elimina el journal; si solo existe el nuevo, completa el cambio: corrige los metadatos y el default cuando hace falta, elimina el overlay anterior y luego ejecuta la acción de Keychain registrada antes de borrar el journal. La recuperación forward ejecuta esa acción mientras mantiene los locks de perfil ordenados, el lock de secuencia y `swap.lock`; si falla, conserva el journal para reintentar en la siguiente operación del almacén. En el camino normal de rename, el callback se ejecuta con los locks de perfil mantenidos, después de liberar el lock de secuencia. Si existen ambos directorios o ninguno, el journal o los metadatos están dañados, o algún lock requerido está ocupado, la recuperación falla de forma segura y conserva el journal y los datos de perfil.

Cuando está activado (por defecto; `--no-backup` lo desactiva), `delete` crea y verifica el ZIP de respaldo antes de borrar los datos del perfil: el respaldo se escribe y verifica manteniendo el lock de perfil, y la fase de confirmación vuelve a tomar el lock de secuencia, revalida la identidad y las rutas del perfil y solo entonces borra. Se conservan como máximo los últimos 5 ZIP por perfil. Para Antigravity, la CLI conserva el lock de perfil durante la purga de llavero posterior al borrado, después de liberar el lock de secuencia; en macOS, la purga usa `swap.lock` para serializar la limpieza del slot guardado y de la entrada del Llavero del sistema con los cambios de credenciales. Si falla la purga, se informa una advertencia después del borrado. `import` mantiene el lock del perfil de destino durante la copia (más `swap.lock` en macOS para Antigravity, para que la credencial importada no compita con un intercambio en curso) y `share-config` mantiene los locks de los perfiles de origen y destino hasta terminar todas las copias. `doctor --fix` mantiene el lock del perfil mientras migra y vuelve a enlazar los datos del overlay.

Las rutas del almacén están blindadas: un symlink o junction en la raíz de perfiles, un directorio de perfil, `profile.json`, `data/`, la raíz de overlays o la ruta de configuración de Claude falla de forma segura en lugar de redirigir una escritura fuera del almacén, y dos perfiles que reclamen la misma secuencia se rechazan en vez de aliasar una configuración de Claude. En macOS, la contención del puente del llavero informa un error limpio para selección explícita/login; la rotación `agy` nunca toma ese lock de intercambio.

| Plataforma | Mecanismo |
|---|---|
| **macOS** | Autenticación privada en disco para rotación `agy`; puente del llavero para selección explícita/login. Los otros motores omiten el puente; Claude gestiona su llavero nativo. |
| **Linux** | `bwrap` opcional (`use_linux_sandbox`) enmascara los sockets de DBus y del keyring. Si falta `bwrap`, avisa y sigue. |
| **Windows** | Junctions NTFS. Sin permisos de administrador y sin modo de desarrollador. |

---

## 🩺 Diagnóstico

`agydra doctor` hace diez chequeos: binarios que de verdad se usan, almacén, perfiles, locks, aislamiento, llavero, canario de esquema, huérfanos, sandbox de Linux y el shim del PATH. `agydra doctor --fix` migra al almacén del perfil un directorio real heredado que ocupa la ruta de datos del overlay de un perfil existente y vuelve a enlazar esa ruta mientras mantiene el lock del perfil; borra el default configurado cuando su nombre no aparece en la lista actual de perfiles con metadatos legibles. Para limpiar huérfanos, el directorio del perfil cuenta como propietario aunque no se puedan leer sus metadatos. Doctor elimina directorios de overlay huérfanos y copias de seguridad de secretos y archivos en cuarentena del llavero solo cuando falta el directorio del perfil propietario; nunca escanea ni borra los ZIP de `backups/`, que `delete` conserva a propósito para recuperación y que solo poda el límite de retención por perfil. Intenta adquirir el lock de mutación de cada perfil candidato y omite la limpieza si una sesión viva (un lock tomado o un lease registrado) usa ese perfil. Los slots del llavero del sistema también se purgan solo cuando falta el directorio del perfil, tras adquirir primero el lock del perfil y luego `swap.lock`. Los archivos del lock de sesión persisten como marcadores y no se eliminan.

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
  "grok_binary": "/usr/local/bin/grok",
  "claude_binary": "/usr/local/bin/claude"
}
```

Todas las claves de binarios (`agy_binary`, `codex_binary`, `grok_binary` y `claude_binary`) son opcionales. Si omites una clave, Agydra usa el `PATH`.

| Variable | Para qué |
|---|---|
| `AGYDRA_PROFILE` | Perfil de respaldo. Ganan `-p` y `.agydra`. |
| `AGYDRA_LANG` | `en` o `es` para un proceso. |
| `AGYDRA_HOME` | Almacén y overlays. |
| `AGYDRA_AGY_BIN` / `AGYDRA_CODEX_BIN` / `AGYDRA_GROK_BIN` / `AGYDRA_CLAUDE_BIN` | Binario alternativo. |
| `AGYDRA_NO_KEYCHAIN` | Omite el puente de llavero de macOS. |
| `NO_COLOR` / `FORCE_COLOR` | Estilo ANSI. |

---

## 🔧 Pruebas

```sh
python3 -W error::ResourceWarning -m pytest tests/ -q
python3 -m unittest discover -s tests -q
```

La suite usa almacenes temporales y deja el home del host en paz. La matriz nativa de CI (`.github/workflows/tests.yml`) corre la suite completa en tres runners: Windows 2022 (Python 3.9), Ubuntu 24.04 (Python 3.9) y macOS 15 (Python 3.14); las versiones intermedias de Python no forman parte de la matriz. Se ejecuta en push y pull request hacia `main` o `audit/project-wide-reliability`.

---

## ⚠️ Limitaciones

- Agydra gestiona perfiles y sesiones. No instala ni actualiza `agy`, `codex`, `grok` ni `claude`.
- El aislamiento sigue a cada CLI: `HOME` para `agy`, `CODEX_HOME` para `codex`, `GROK_HOME` más `GROK_LEADER_SOCKET` para `grok`, y `CLAUDE_CONFIG_DIR` para `claude`. El canario de esquema de Doctor avisa si el diseño no coincide con los drivers actuales.
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
