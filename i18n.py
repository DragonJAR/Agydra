"""Internacionalización para Agydra (stdlib-only, Python >= 3.9).

Soporta dos idiomas: ``'en'`` (inglés, predeterminado) y ``'es'`` (español).

Cascada de resolución de idioma (de mayor a menor prioridad):
  1. Argumento ``flag_lang`` (p.ej. ``--lang es`` en la CLI).
  2. Variable de entorno ``AGYDRA_LANG``.
  3. ``config.settings['lang']`` en ``agydra.json`` (persistido por :func:`set_language`).
  4. Locale del sistema vía ``locale.getlocale()`` — si empieza por ``'es'`` → ``'es'``;
     cualquier otro → ``'en'``.

Uso::

    import i18n
    i18n.resolve_language(store, flag_lang=args.lang)   # una sola vez al arranque
    print(i18n.t("profile.created", name="work"))        # cadenas con placeholders

Convenciones de claves:
  - Punto como separador de espacio de nombres (``"cmd.create.help"``).
  - Minúsculas y guión bajo; sin espacios.
  - El argumento ``default`` de :func:`t` acepta una cadena de fallback ad-hoc
    sin necesidad de agregar la clave a los diccionarios.
"""
from __future__ import annotations

import locale
import os
import threading
from typing import Optional

SUPPORTED_LANGS = ('en', 'es')
DEFAULT_LANG = 'en'

_LANG_ENV = "AGYDRA_LANG"
_SETTINGS_KEY = "lang"

_STRINGS: dict[str, dict[str, str]] = {
    'en': {
        'lang.name': 'English',
        'lang.set_ok': 'Language set to English and saved.',
        'lang.unknown': 'Unknown language {lang!r}. Supported: {supported}.',
        'lang.env_override': 'Language forced by AGYDRA_LANG={lang}.',
        'launcher.description': 'agydra — profile manager for agy and codex.\n\nRun without arguments to launch agy with the default profile.\nUse -p <name> to pick a specific one, -r to pick a free one.',
        'launcher.hint_no_profiles': 'No profiles yet. Create one with: agydra create <name>',
        'launcher.profile_busy': 'Profile {name!r} is busy: another live session is using it.',
        'launcher.no_free_profile': 'No free authenticated profile available. Use -r or create a new one with: agydra create <name>',
        'launcher.dry_run_header': '--- dry-run plan ---',
        'launcher.force_warn': 'Forcing launch on profile {name!r} without lock: concurrent sessions may corrupt OAuth tokens (--force opt-in).',
        'cmd.create.help': 'Create a new profile.',
        'cmd.create.ok': 'Profile {name!r} created.',
        'cmd.create.already_exists': 'Profile {name!r} already exists.',
        'cmd.create.invalid_name': "Invalid profile name {name!r}. Use lowercase letters, digits, '-' and '_' (max 64 chars).",
        'cmd.create.reserved_name': 'Name {name!r} is reserved (matches a subcommand).',
        'cmd.create.engine_invalid': 'Unknown engine {engine!r}. Supported engines: {supported}.',
        'cmd.login.help': 'Log in with a profile (launch the CLI for authentication).',
        'cmd.login.launching': 'Launching {engine} login for profile {name!r}…',
        'cmd.login.ok': 'Login flow completed for profile {name!r}.',
        'cmd.import.help': 'Import an existing agy/codex data directory into a new profile.',
        'cmd.import.ok': 'Profile {name!r} imported from {path}.',
        'cmd.import.not_found': 'Source path not found: {path}',
        'cmd.import.already_exists': 'Profile {name!r} already exists. Use a different name.',
        'cmd.list.help': 'List all profiles.',
        'cmd.list.no_profiles': 'No profiles yet. Create one with: agydra create <name>',
        'cmd.list.header_name': 'NAME',
        'cmd.list.header_engine': 'ENGINE',
        'cmd.list.header_state': 'STATE',
        'cmd.list.header_email': 'EMAIL',
        'cmd.list.header_last_used': 'LAST USED',
        'cmd.list.state_authenticated': 'authenticated',
        'cmd.list.state_not_authenticated': 'not authenticated',
        'cmd.list.state_busy': 'busy',
        'cmd.list.default_marker': '(default)',
        'cmd.list.never_used': 'never',
        'cmd.status.help': 'Show detailed status of a profile.',
        'cmd.status.not_found': 'Profile {name!r} not found.',
        'cmd.status.state': 'State   : {state}',
        'cmd.status.email': 'Email   : {email}',
        'cmd.status.engine': 'Engine  : {engine}',
        'cmd.status.created': 'Created : {created}',
        'cmd.status.last_used': 'Last use: {last_used}',
        'cmd.status.never_used': 'never',
        'cmd.status.data_dir': 'Data dir: {path}',
        'cmd.status.overlay': 'Overlay : {path}',
        'cmd.default.help': 'Set or show the default profile.',
        'cmd.default.set_ok': 'Default profile set to {name!r}.',
        'cmd.default.show': 'Default profile: {name}',
        'cmd.default.none': 'No default profile is set.',
        'cmd.default.not_found': 'Profile {name!r} not found.',
        'cmd.use.help': "Alias for 'default': set the default profile.",
        'cmd.rename.help': 'Rename a profile.',
        'cmd.rename.ok': 'Profile {old!r} renamed to {new!r}.',
        'cmd.rename.not_found': 'Profile {name!r} not found.',
        'cmd.rename.new_exists': 'A profile named {name!r} already exists.',
        'cmd.rename.same_name': 'Old and new name are the same ({name!r}).',
        'cmd.delete.help': 'Delete a profile (backs up data first).',
        'cmd.delete.confirm': 'Delete profile {name!r}? This cannot be undone. [y/N] ',
        'cmd.delete.aborted': 'Aborted.',
        'cmd.delete.ok': 'Profile {name!r} deleted. Backup saved to {path}',
        'cmd.delete.not_found': 'Profile {name!r} not found.',
        'cmd.delete.busy': 'Profile {name!r} is in use. Stop the session first or pass -f/--force to override.',
        'cmd.delete.backup_note': 'Backup: {path}',
        'cmd.share_config.help': 'Copy settings from one profile to another.',
        'cmd.share_config.ok': 'Settings copied from {src!r} to {dst!r}.',
        'cmd.share_config.not_found': 'Profile {name!r} not found.',
        'cmd.doctor.help': 'Run diagnostics and optionally repair the store.',
        'cmd.doctor.fix_preview_header': 'The following issues can be repaired:',
        'cmd.doctor.fix_confirm': 'Apply fixes? [y/N] ',
        'cmd.doctor.fix_aborted': 'No changes made.',
        'cmd.doctor.fix_done': 'Fixes applied.',
        'cmd.doctor.result_ok': 'result: healthy',
        'cmd.doctor.result_fail': 'result: FAIL — fix the [XX] items above',
        'cmd.doctor.legend': 'legend:',
        'cmd.doctor.legend_ok': '[ok]=pass',
        'cmd.doctor.legend_warn': '[!!]=warn',
        'cmd.doctor.legend_fail': '[XX]=fail',
        'cmd.usage.help': 'Show quota / token usage for the active profile.',
        'cmd.usage.header': 'Usage for profile {name!r} ({engine}):',
        'cmd.usage.not_found': 'Profile {name!r} not found.',
        'cmd.usage.fetch_error': 'Could not retrieve usage: {error}',
        'cmd.usage.no_data': 'No usage data available yet.',
        'cmd.usage.bar_label': 'Tokens used',
        'cmd.setup.help': 'Install or update the agydra shim and venv.',
        'cmd.setup.ok': 'agydra installed successfully.',
        'cmd.setup.already_ok': 'agydra is already up to date.',
        'cmd.setup.dry_run_header': '--- dry-run: would perform ---',
        'cmd.setup.foreign_shim': "Foreign file at {path}. Inspect and remove it, or re-run 'agydra setup --force'.",
        'cmd.lang.help': 'Set the display language (en / es).',
        'cmd.lang.ok': 'Language set to {lang!r} and saved.',
        'cmd.lang.unknown': 'Unknown language {lang!r}. Supported: {supported}.',
        'cmd.lang.current': 'Current language: {lang}',
        'cmd.version.help': 'Print the agydra version and exit.',
        'cmd.help.help': 'Show this help message and exit.',
        'auth.authenticated': 'authenticated',
        'auth.not_authenticated': 'not authenticated',
        'auth.unknown': 'unknown',
        'doctor.binary.ok': 'agy binary: {path}',
        'doctor.binary.fail': 'agy binary not found (install agy or set {env_var}).',
        'doctor.binary.codex_ok': 'codex binary: {path}',
        'doctor.binary.codex_fail': 'codex binary not found (install codex or set {env_var}).',
        'doctor.binary.grok_ok': 'grok binary: {path}',
        'doctor.binary.grok_fail': 'grok binary not found (install grok or set {env_var}).',
        'doctor.store.ok': 'store writable: {path}',
        'doctor.store.fail': 'store not writable: {path} ({error})',
        'doctor.profiles.count': 'profiles: {count}',
        'doctor.profiles.missing_data_dir': '{name}: data dir missing ({path})',
        'doctor.profiles.default_missing': 'default profile {name!r} does not exist (fix with: agydra default <name>)',
        'doctor.profiles.no_profiles': 'no profiles yet (create one with: agydra create <name>)',
        'doctor.profiles.unreadable': 'unreadable profile metadata: {names} (remove with: agydra delete <name>)',
        'doctor.locks.no_profiles': 'locks: no profiles to check',
        'doctor.locks.live': 'locks: live sessions: {names}',
        'doctor.locks.none': 'locks: no live sessions',
        'doctor.locks.error': 'locks: cannot inspect ({error})',
        'doctor.isolation.no_profiles': 'isolation not checked (no profiles)',
        'doctor.isolation.ok': 'isolation ok for {count} profile(s){note}',
        'doctor.isolation.pending': 'isolation not yet verifiable (no profile launched): {names}',
        'doctor.isolation.broken': 'isolation broken: {details}',
        'doctor.isolation.recoverable': "isolation recoverable: real directory at overlay data-link for {names} (data intact — run 'agydra doctor --fix' to migrate it into the profile store and relink)",
        'doctor.isolation.points_to_real': '{name}: overlay {link} points to REAL store',
        'doctor.isolation.points_elsewhere': '{name}: overlay {link} points elsewhere',
        'doctor.keychain.na': 'keychain bridge: n/a (file-backed credentials on this OS)',
        'doctor.keychain.missing_security': "keychain bridge: 'security' not found (swap disabled)",
        'doctor.keychain.disabled_env': 'keychain bridge: bridge disabled via AGYDRA_NO_KEYCHAIN',
        'doctor.keychain.status': 'keychain bridge: shared slot {shared}; per-profile slots: {slots}',
        'doctor.keychain.skip_marker': 'keychain setup skipped ({marker} marker present at {path}): automatic login keychain creation previously failed or was cancelled; delete this marker file to retry keychain initialization',
        'doctor.keychain.mismatch': 'identity mismatch: {details}',
        'doctor.keychain.orphans': "orphaned keychain slots: {names} (run 'agydra doctor --fix' to purge)",
        'doctor.keychain.bad_format': 'shared slot payload is not valid JSON (format: {format!r}) — agy may ask you to log in again; launching agy once through agydra self-heals it',
        'doctor.schema.pending': 'schema canary pending (no profiles to inspect)',
        'doctor.schema.ok': 'profile stores contain agy data layout (schema canary passed)',
        'doctor.schema.missing': "no agy data layout found in profiles yet; run 'agydra login <profile>' and re-run doctor to confirm tokens land in the profile store",
        'doctor.orphans.none': 'orphans: none found',
        'doctor.orphans.found': 'orphaned store artifacts found (see: agydra doctor --fix)',
        'doctor.sandbox.na': 'linux sandbox: n/a (not linux)',
        'doctor.sandbox.ok': 'linux sandbox: bwrap available',
        'doctor.sandbox.missing': 'linux sandbox: bwrap not installed (optional hardening disabled)',
        'doctor.install.ok': 'install: {details}',
        'doctor.install.venv_missing': "install: venv missing — run 'python3 agydra.py' or 'agydra setup'",
        'doctor.install.script_missing': "install: console script missing — re-run 'agydra setup'",
        'doctor.install.shim_foreign': "install: foreign file at {path} — inspect and remove it, or re-run 'agydra setup --force'",
        'error.store': 'Store error: {error}',
        'error.profile_not_found': 'Profile {name!r} not found.',
        'error.profile_name_invalid': 'Invalid profile name: {error}',
        'error.binary_not_found': 'Could not find the {binary} binary. Install it or point agydra to it with --binary <path>.',
        'error.isolation': 'Isolation error: {error}',
        'error.unexpected': 'Unexpected error: {error}',
        'error.permission_denied': 'Permission denied: {path}',
        'error.config_corrupt': 'Ignoring corrupt config ({error}); using defaults until it is fixed or deleted.',
        'confirm.yes_no': '[y/N] ',
        'confirm.aborted': 'Aborted.',
        'confirm.proceed': 'Proceed? [y/N] ',
        'banner.tagline': 'profile manager for agy, codex, and grok',
        'banner.version': 'version {version}',
        'usage.no_session': 'No active session detected for profile {name!r}.',
        'usage.fetching': 'Fetching usage for profile {name!r}…',
        'usage.requests': 'Requests  : {used} / {limit}',
        'usage.tokens': 'Tokens    : {used} / {limit}',
        'usage.reset': 'Resets    : {date}',
        'usage.unlimited': 'unlimited',
        'usage.unavailable': 'unavailable',
        'usage.header_profile': 'PROFILE',
        'usage.header_account': 'ACCOUNT',
        'usage.header_available': 'AVAILABLE',
        'usage.header_windows': 'WK · 5H',
        'usage.header_plan': 'PLAN',
        'usage.header_status': 'STATUS',
        'usage.not_eligible': '✗ not eligible',
        'usage.recommend_gemini': '▸ Use for Gemini: {profile} ({pct}%)',
        'usage.recommend_claude': '▸ Use for Claude/GPT: {profile} ({pct}%)',
        'usage.ineligible_note': '✗ {profile}: account not eligible for Antigravity. Needs verification.',
        'usage.section_codex': 'OPENAI CODEX',
        'usage.section_grok': 'XAI GROK',
        'usage.section_agy': 'ANTIGRAVITY',
        'usage.use_now': 'USE NOW',
        'usage.profiles_label': 'profiles',
    },
    'es': {
        'lang.name': 'Español',
        'lang.set_ok': 'Idioma cambiado a Español y guardado.',
        'lang.unknown': 'Idioma desconocido {lang!r}. Soportados: {supported}.',
        'lang.env_override': 'Idioma forzado por AGYDRA_LANG={lang}.',
        'launcher.description': 'agydra — gestor de perfiles para agy y codex.\n\nSin argumentos lanza agy con el perfil predeterminado.\nUsa -p <nombre> para elegir uno, -r para tomar uno libre.',
        'launcher.hint_no_profiles': 'Aún no hay perfiles. Crea uno con: agydra create <nombre>',
        'launcher.profile_busy': 'El perfil {name!r} está ocupado: otra sesión activa lo está usando.',
        'launcher.no_free_profile': 'No hay ningún perfil autenticado y libre. Usa -r o crea uno nuevo con: agydra create <nombre>',
        'launcher.dry_run_header': '--- plan simulado (dry-run) ---',
        'launcher.force_warn': 'Lanzamiento forzado sobre el perfil {name!r} sin bloqueo: las sesiones concurrentes pueden corromper los tokens OAuth (opción --force).',
        'cmd.create.help': 'Crear un perfil nuevo.',
        'cmd.create.ok': 'Perfil {name!r} creado.',
        'cmd.create.already_exists': 'El perfil {name!r} ya existe.',
        'cmd.create.invalid_name': "Nombre de perfil no válido {name!r}. Usa letras minúsculas, dígitos, '-' y '_' (máx. 64 caracteres).",
        'cmd.create.reserved_name': 'El nombre {name!r} está reservado (coincide con un subcomando).',
        'cmd.create.engine_invalid': 'Motor desconocido {engine!r}. Motores soportados: {supported}.',
        'cmd.login.help': 'Iniciar sesión con un perfil (lanza la CLI para autenticarse).',
        'cmd.login.launching': 'Iniciando sesión de {engine} para el perfil {name!r}…',
        'cmd.login.ok': 'Flujo de inicio de sesión completado para el perfil {name!r}.',
        'cmd.import.help': 'Importar un directorio de datos de agy/codex a un perfil nuevo.',
        'cmd.import.ok': 'Perfil {name!r} importado desde {path}.',
        'cmd.import.not_found': 'Ruta de origen no encontrada: {path}',
        'cmd.import.already_exists': 'El perfil {name!r} ya existe. Elige otro nombre.',
        'cmd.list.help': 'Listar todos los perfiles.',
        'cmd.list.no_profiles': 'Aún no hay perfiles. Crea uno con: agydra create <nombre>',
        'cmd.list.header_name': 'NOMBRE',
        'cmd.list.header_engine': 'MOTOR',
        'cmd.list.header_state': 'ESTADO',
        'cmd.list.header_email': 'CORREO',
        'cmd.list.header_last_used': 'ÚLTIMO USO',
        'cmd.list.state_authenticated': 'autenticado',
        'cmd.list.state_not_authenticated': 'no autenticado',
        'cmd.list.state_busy': 'ocupado',
        'cmd.list.default_marker': '(predeterminado)',
        'cmd.list.never_used': 'nunca',
        'cmd.status.help': 'Mostrar el estado detallado de un perfil.',
        'cmd.status.not_found': 'Perfil {name!r} no encontrado.',
        'cmd.status.state': 'Estado   : {state}',
        'cmd.status.email': 'Correo   : {email}',
        'cmd.status.engine': 'Motor    : {engine}',
        'cmd.status.created': 'Creado   : {created}',
        'cmd.status.last_used': 'Último uso: {last_used}',
        'cmd.status.never_used': 'nunca',
        'cmd.status.data_dir': 'Dir. datos: {path}',
        'cmd.status.overlay': 'Overlay  : {path}',
        'cmd.default.help': 'Establecer o mostrar el perfil predeterminado.',
        'cmd.default.set_ok': 'Perfil predeterminado establecido en {name!r}.',
        'cmd.default.show': 'Perfil predeterminado: {name}',
        'cmd.default.none': 'No hay perfil predeterminado configurado.',
        'cmd.default.not_found': 'Perfil {name!r} no encontrado.',
        'cmd.use.help': "Alias de 'default': establece el perfil predeterminado.",
        'cmd.rename.help': 'Renombrar un perfil.',
        'cmd.rename.ok': 'Perfil {old!r} renombrado a {new!r}.',
        'cmd.rename.not_found': 'Perfil {name!r} no encontrado.',
        'cmd.rename.new_exists': 'Ya existe un perfil llamado {name!r}.',
        'cmd.rename.same_name': 'El nombre antiguo y el nuevo son iguales ({name!r}).',
        'cmd.delete.help': 'Eliminar un perfil (hace una copia de seguridad primero).',
        'cmd.delete.confirm': '¿Eliminar el perfil {name!r}? Esta acción no se puede deshacer. [s/N] ',
        'cmd.delete.aborted': 'Cancelado.',
        'cmd.delete.ok': 'Perfil {name!r} eliminado. Copia guardada en {path}',
        'cmd.delete.not_found': 'Perfil {name!r} no encontrado.',
        'cmd.delete.busy': 'El perfil {name!r} está en uso. Detén la sesión primero o usa -f/--force para omitir el bloqueo.',
        'cmd.delete.backup_note': 'Copia de seguridad: {path}',
        'cmd.share_config.help': 'Copiar la configuración de un perfil a otro.',
        'cmd.share_config.ok': 'Configuración copiada de {src!r} a {dst!r}.',
        'cmd.share_config.not_found': 'Perfil {name!r} no encontrado.',
        'cmd.doctor.help': 'Ejecutar diagnósticos y opcionalmente reparar el almacén.',
        'cmd.doctor.fix_preview_header': 'Los siguientes problemas pueden repararse:',
        'cmd.doctor.fix_confirm': '¿Aplicar reparaciones? [s/N] ',
        'cmd.doctor.fix_aborted': 'Sin cambios.',
        'cmd.doctor.fix_done': 'Reparaciones aplicadas.',
        'cmd.doctor.result_ok': 'resultado: saludable',
        'cmd.doctor.result_fail': 'resultado: FALLO — corrige los elementos [XX] de arriba',
        'cmd.doctor.legend': 'leyenda:',
        'cmd.doctor.legend_ok': '[ok]=correcto',
        'cmd.doctor.legend_warn': '[!!]=advertencia',
        'cmd.doctor.legend_fail': '[XX]=fallo',
        'cmd.usage.help': 'Mostrar el uso de cuota / tokens del perfil activo.',
        'cmd.usage.header': 'Uso para el perfil {name!r} ({engine}):',
        'cmd.usage.not_found': 'Perfil {name!r} no encontrado.',
        'cmd.usage.fetch_error': 'No se pudo obtener el uso: {error}',
        'cmd.usage.no_data': 'Aún no hay datos de uso disponibles.',
        'cmd.usage.bar_label': 'Tokens usados',
        'cmd.setup.help': 'Instalar o actualizar el shim y el entorno virtual de agydra.',
        'cmd.setup.ok': 'agydra instalado correctamente.',
        'cmd.setup.already_ok': 'agydra ya está actualizado.',
        'cmd.setup.dry_run_header': '--- simulación: se realizaría ---',
        'cmd.setup.foreign_shim': "Archivo ajeno en {path}. Inspecciónalo y elimínalo, o vuelve a ejecutar 'agydra setup --force'.",
        'cmd.lang.help': 'Establecer el idioma de la interfaz (en / es).',
        'cmd.lang.ok': 'Idioma establecido en {lang!r} y guardado.',
        'cmd.lang.unknown': 'Idioma desconocido {lang!r}. Soportados: {supported}.',
        'cmd.lang.current': 'Idioma actual: {lang}',
        'cmd.version.help': 'Mostrar la versión de agydra y salir.',
        'cmd.help.help': 'Mostrar este mensaje de ayuda y salir.',
        'auth.authenticated': 'autenticado',
        'auth.not_authenticated': 'no autenticado',
        'auth.unknown': 'desconocido',
        'doctor.binary.ok': 'binario agy: {path}',
        'doctor.binary.fail': 'binario agy no encontrado (instala agy o define {env_var}).',
        'doctor.binary.codex_ok': 'binario codex: {path}',
        'doctor.binary.codex_fail': 'binario codex no encontrado (instala codex o define {env_var}).',
        'doctor.binary.grok_ok': 'binario grok: {path}',
        'doctor.binary.grok_fail': 'binario grok no encontrado (instala grok o define {env_var}).',
        'doctor.store.ok': 'almacén con escritura: {path}',
        'doctor.store.fail': 'almacén sin escritura: {path} ({error})',
        'doctor.profiles.count': 'perfiles: {count}',
        'doctor.profiles.missing_data_dir': '{name}: directorio de datos faltante ({path})',
        'doctor.profiles.default_missing': 'el perfil predeterminado {name!r} no existe (corrige con: agydra default <nombre>)',
        'doctor.profiles.no_profiles': 'aún no hay perfiles (crea uno con: agydra create <nombre>)',
        'doctor.profiles.unreadable': 'metadatos de perfil ilegibles: {names} (elimina con: agydra delete <nombre>)',
        'doctor.locks.no_profiles': 'bloqueos: no hay perfiles que verificar',
        'doctor.locks.live': 'bloqueos: sesiones activas: {names}',
        'doctor.locks.none': 'bloqueos: sin sesiones activas',
        'doctor.locks.error': 'bloqueos: no se pueden inspeccionar ({error})',
        'doctor.isolation.no_profiles': 'aislamiento no verificado (sin perfiles)',
        'doctor.isolation.ok': 'aislamiento correcto para {count} perfil(es){note}',
        'doctor.isolation.pending': 'aislamiento aún no verificable (ningún perfil lanzado): {names}',
        'doctor.isolation.broken': 'aislamiento roto: {details}',
        'doctor.isolation.recoverable': "aislamiento recuperable: directorio real en el enlace del overlay para {names} (datos intactos — ejecuta 'agydra doctor --fix' para migrar al almacén del perfil y reenlazar)",
        'doctor.isolation.points_to_real': '{name}: overlay {link} apunta al almacén REAL',
        'doctor.isolation.points_elsewhere': '{name}: overlay {link} apunta a otro lugar',
        'doctor.keychain.na': 'puente de llavero: no aplica (credenciales en archivo en este SO)',
        'doctor.keychain.missing_security': "puente de llavero: comando 'security' no encontrado (intercambio deshabilitado)",
        'doctor.keychain.disabled_env': 'puente de llavero: deshabilitado vía AGYDRA_NO_KEYCHAIN',
        'doctor.keychain.status': 'puente de llavero: ranura compartida {shared}; ranuras por perfil: {slots}',
        'doctor.keychain.skip_marker': 'configuración del llavero omitida (marcador {marker} presente en {path}): la creación automática del llavero de inicio de sesión falló o fue cancelada; elimina este archivo marcador para reintentar la inicialización',
        'doctor.keychain.mismatch': 'discrepancia de identidad: {details}',
        'doctor.keychain.orphans': "ranuras huérfanas del llavero: {names} (ejecuta 'agydra doctor --fix' para purgar)",
        'doctor.keychain.bad_format': 'la ranura compartida no contiene JSON válido (formato: {format!r}) — agy podría pedirte que inicies sesión de nuevo; lanzar agy una vez a través de agydra lo autocorrige',
        'doctor.schema.pending': 'verificación de esquema pendiente (sin perfiles que inspeccionar)',
        'doctor.schema.ok': 'los almacenes de perfil contienen el esquema de datos de agy (verificación pasada)',
        'doctor.schema.missing': "no se encontró el esquema de datos de agy en ningún perfil; ejecuta 'agydra login <perfil>' y vuelve a correr doctor para confirmar que los tokens quedan en el almacén del perfil",
        'doctor.orphans.none': 'huérfanos: ninguno encontrado',
        'doctor.orphans.found': 'artefactos huérfanos encontrados en el almacén (ver: agydra doctor --fix)',
        'doctor.sandbox.na': 'sandbox linux: no aplica (no es linux)',
        'doctor.sandbox.ok': 'sandbox linux: bwrap disponible',
        'doctor.sandbox.missing': 'sandbox linux: bwrap no instalado (endurecimiento opcional deshabilitado)',
        'doctor.install.ok': 'instalación: {details}',
        'doctor.install.venv_missing': "instalación: venv faltante — ejecuta 'python3 agydra.py' o 'agydra setup'",
        'doctor.install.script_missing': "instalación: script de consola faltante — vuelve a ejecutar 'agydra setup'",
        'doctor.install.shim_foreign': "instalación: archivo ajeno en {path} — inspecciónalo y elimínalo, o vuelve a ejecutar 'agydra setup --force'",
        'error.store': 'Error en el almacén: {error}',
        'error.profile_not_found': 'Perfil {name!r} no encontrado.',
        'error.profile_name_invalid': 'Nombre de perfil no válido: {error}',
        'error.binary_not_found': 'No se encontró el binario {binary}. Instálalo o indícale la ruta a agydra con --binary <ruta>.',
        'error.isolation': 'Error de aislamiento: {error}',
        'error.unexpected': 'Error inesperado: {error}',
        'error.permission_denied': 'Permiso denegado: {path}',
        'error.config_corrupt': 'Ignorando configuración corrupta ({error}); usando valores predeterminados hasta que se corrija o elimine.',
        'confirm.yes_no': '[s/N] ',
        'confirm.aborted': 'Cancelado.',
        'confirm.proceed': '¿Continuar? [s/N] ',
        'banner.tagline': 'gestor de perfiles para agy, codex y grok',
        'banner.version': 'versión {version}',
        'usage.no_session': 'No se detectó sesión activa para el perfil {name!r}.',
        'usage.fetching': 'Obteniendo uso para el perfil {name!r}…',
        'usage.requests': 'Solicitudes: {used} / {limit}',
        'usage.tokens': 'Tokens     : {used} / {limit}',
        'usage.reset': 'Renovación : {date}',
        'usage.unlimited': 'ilimitado',
        'usage.unavailable': 'no disponible',
        'usage.header_profile': 'PERFIL',
        'usage.header_account': 'CUENTA',
        'usage.header_available': 'DISPONIBLE',
        'usage.header_windows': 'SEM · 5H',
        'usage.header_plan': 'PLAN',
        'usage.header_status': 'ESTADO',
        'usage.not_eligible': '✗ no elegible',
        'usage.recommend_gemini': '▸ Usar para Gemini: {profile} ({pct}%)',
        'usage.recommend_claude': '▸ Usar para Claude/GPT: {profile} ({pct}%)',
        'usage.ineligible_note': '✗ {profile}: cuenta no elegible para Antigravity. Hay que verificarla.',
        'usage.section_codex': 'OPENAI CODEX',
        'usage.section_grok': 'XAI GROK',
        'usage.section_agy': 'ANTIGRAVITY',
        'usage.use_now': 'USAR AHORA',
        'usage.profiles_label': 'perfiles',
    },
}

_lock = threading.Lock()
_active_lang = 'en'


def _set_active(lang: str) -> None:
    """Escribe el idioma activo de forma atómica."""
    global _active_lang
    with _lock:
        _active_lang = lang


def _get_active() -> str:
    """Lee el idioma activo de forma atómica."""
    with _lock:
        return _active_lang


def t(key: str, default: Optional[str] = None, **kwargs: object) -> str:
    """Devuelve la cadena localizada para ``key`` en el idioma activo.

Cascada de fallback:
  1. Idioma activo.
  2. Idioma predeterminado (``'en'``).
  3. ``default`` si se proporcionó.
  4. La propia ``key`` (nunca lanza).

``**kwargs`` se interpolan con ``str.format_map`` — errores de
interpolación devuelven la plantilla sin sustituir (fail-open).

Ejemplo::

    t("cmd.create.ok", name="work")
    # → "Profile 'work' created."  (en)
    # → "Perfil 'work' creado."    (es)
"""
    lang = _get_active()
    template = (
        _STRINGS.get(lang, {}).get(key)
        or _STRINGS.get(DEFAULT_LANG, {}).get(key)
        or default
        or key
    )
    if not kwargs:
        return template
    try:
        return template.format_map(kwargs)
    except (KeyError, ValueError, IndexError):
        return template


def _normalize(lang: str) -> Optional[str]:
    """Normaliza y valida un código de idioma; retorna ``None`` si no es soportado."""
    normalized = lang.strip().lower()
    if normalized in SUPPORTED_LANGS:
        return normalized
    return None


def _locale_lang() -> str:
    """Detecta el idioma del sistema vía ``locale.getlocale()``.

Retorna ``'es'`` si el locale empieza por ``'es'``; ``'en'`` en cualquier
otro caso (incluyendo locales no determinados o errores de la API).
"""
    try:
        loc, _ = locale.getlocale()
        if loc and loc.lower().startswith("es"):
            return "es"
        return DEFAULT_LANG
    except Exception:
        return DEFAULT_LANG


def resolve_language(store: object = None, flag_lang: Optional[str] = None) -> str:
    """Determina el idioma activo y lo establece como estado global.

Cascada (de mayor a menor prioridad):
  1. ``flag_lang`` — argumento explícito de la CLI (``--lang``).
  2. Variable de entorno ``AGYDRA_LANG``.
  3. ``config.settings['lang']`` en ``agydra.json``.
  4. Locale del sistema.

Valores no soportados en cualquier nivel se ignoran silenciosamente y
la cascada continúa hacia el siguiente nivel.  El idioma resuelto se
establece como activo (efecto de lado único y controlado: llama a
:func:`_set_active`).

Retorna el código del idioma resuelto (``'en'`` o ``'es'``).
"""
    if flag_lang:
        resolved = _normalize(flag_lang)
        if resolved:
            _set_active(resolved)
            return resolved
    env_val = os.environ.get(_LANG_ENV)
    if env_val:
        resolved = _normalize(env_val)
        if resolved:
            _set_active(resolved)
            return resolved
    if store is not None:
        try:
            config = store.load_config()
            settings_lang = config.settings.get(_SETTINGS_KEY)
            resolved = _normalize(str(settings_lang)) if settings_lang else None
            if resolved is None:
                legacy = config.settings.get("language")
                if legacy:
                    resolved = _normalize(str(legacy))
            if resolved:
                _set_active(resolved)
                return resolved
        except Exception:
            pass
    resolved = _locale_lang()
    _set_active(resolved)
    return resolved


def set_language(store: object, lang: str) -> None:
    """Valida ``lang`` y lo persiste atómicamente en ``agydra.json``.

Usa el mecanismo atómico de escritura del store (``save_config`` +
``os.replace`` interno) para que ningún crash deje el archivo corrupto.

Lanza ``ValueError`` si ``lang`` no es un idioma soportado.
Lanza ``store.StoreError`` si ``save_config`` falla (archivo corrupto,
permisos, etc.) — el estado global en memoria NO se actualiza en ese
caso: la sesión actual sigue con el idioma anterior y la persistencia
quedó intacta.

Tras una persistencia exitosa también actualiza el estado global en
memoria para que el resto de la sesión vea el idioma nuevo sin
necesidad de reiniciar.
"""
    normalized = _normalize(lang)
    if normalized is None:
        supported_str = ", ".join(repr(s) for s in SUPPORTED_LANGS)
        raise ValueError(t("lang.unknown", lang=lang, supported=supported_str))
    config = store.load_config()
    config.settings[_SETTINGS_KEY] = normalized
    store.save_config(config)
    _set_active(normalized)


def format_usage_timestamp(dt: Optional[object] = None) -> str:
    """Format a localized, clean timestamp: e.g. 'dom 27 sep · 14:32' or 'Sun Sep 27 · 14:32'."""
    from datetime import datetime
    if dt is None:
        dt = datetime.now()
    lang = _get_active()
    if lang == 'es':
        days = ['lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom']
        months = ['', 'ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
        day_str = days[dt.weekday()]
        mo_str = months[dt.month]
        return f"{day_str} {dt.day} {mo_str} · {dt.strftime('%H:%M')}"
    else:
        days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        months = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        day_str = days[dt.weekday()]
        mo_str = months[dt.month]
        return f"{day_str} {mo_str} {dt.day} · {dt.strftime('%H:%M')}"

