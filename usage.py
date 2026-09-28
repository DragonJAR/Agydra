"""Aggregate quota usage across profiles via agy's built-in ``/usage`` query.

Mechanism (validated live against a real agy 1.2.7 binary): running
``agy --print "/usage" --output-format json`` is a stateless, read-only
quota query. It needs no TTY and no interactive prompt, and it returns
clean structured JSON on stdout under ``command.data.groups`` -- a list of
model groups, each holding one or more usage buckets (``weekly``/``5h``
windows) with a ``remaining_fraction`` (0..1) and a UTC ``reset_time``.

Never hardcode group/bucket NAMES ("Gemini"/"Claude") as string literals
anywhere in this module: everything is read dynamically from
``group["name"]``/``bucket["window"]``/``bucket["id"]`` so a future agy
release that adds or renames a model group still renders correctly.

Concurrency-safety note (also validated live): a single ``/usage`` query
is safe to run concurrently with an ALREADY-RUNNING agy session for the
SAME profile -- it is a stateless read, not a conversation turn -- which
is why ``query_profile_usage`` deliberately does NOT take the session
lock (``locks.try_lock``). ``usage`` must keep working while a profile is
busy; this is the one documented exception alongside ``-f/--force`` (see
AGENTS.md's lock-exceptions bullet).

Keychain-safety note: on macOS, when the profile being queried already
owns the shared keychain slot (a real session for it is already running
and put its own credential there), ``keychain.launch_guard`` recognizes
that and becomes a true no-op -- it swaps nothing in and restores nothing
on exit, so ``usage`` genuinely stays a pure read against a busy profile.
Only when the shared slot currently holds a DIFFERENT profile's
credential does the guard still perform its existing, ``swap.lock``-
serialized swap-and-restore around this call.

Sequential-across-PROFILES is a different matter, and a CORRECTNESS
requirement, not a style choice: there is exactly one shared macOS
keychain slot (``svce=gemini``/``acct=antigravity``) that
``keychain.launch_guard`` swaps into and restores for the duration of one
query. Running two ``query_profile_usage`` calls for DIFFERENT profiles
concurrently would race on that single slot -- profile B's swap could
land while profile A's subprocess is still reading it, so A would observe
B's account, under B's credential, and vice versa. ``gather_usage_report``
below is the ONLY place that iterates multiple profiles; it MUST call
``query_profile_usage`` one profile at a time, never via a thread pool,
``asyncio``, or ``concurrent.futures``. Do not "optimize" this loop.

Every failure mode this module can hit -- not authenticated, no agy
binary, an isolation error building the overlay, a keychain swap issue
outside the fail-open contract, a subprocess timeout, a non-zero exit, a
non-JSON stdout body, a ``status`` other than ``"SUCCESS"``, or a
response missing ``command.data`` -- degrades to
``UsageResult(ok=False, error=...)``. ``query_profile_usage`` never
raises, so one broken or ineligible profile can never abort a
multi-profile report.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

import isolation
import keychain
import platforms
import resolver
from account import auth_state

DEFAULT_TIMEOUT_S = 20

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class UsageBucket:
    id: str
    name: str
    window: str
    remaining_fraction: float
    reset_time: Optional[datetime]


@dataclass
class UsageGroup:
    name: str
    buckets: List[UsageBucket] = field(default_factory=list)


@dataclass
class UsageResult:
    name: str
    ok: bool
    groups: List[UsageGroup] = field(default_factory=list)
    error: Optional[str] = None
    engine: str = "agy"
    email: Optional[str] = None
    plan: Optional[str] = None
    is_ineligible: bool = False


@dataclass
class BucketColumn:
    """One column of the compact multi-profile table: a bucket ``id`` plus
    its precomputed, dynamically-derived header text."""

    id: str
    header: str


def _parse_reset_time(raw: object) -> Optional[datetime]:
    """Decode an ISO-8601 UTC ``reset_time`` (``...Z`` suffix).

    ``datetime.fromisoformat`` only accepts the trailing ``Z`` shorthand
    starting with Python 3.11; this project's floor is 3.9, so the ``Z``
    is normalized to ``+00:00`` by hand before parsing. Malformed/missing
    values degrade to ``None`` rather than raising -- callers treat a
    bucket with no reset time as "unknown", never a hard error.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_bucket(raw: object) -> Optional[UsageBucket]:
    if not isinstance(raw, dict):
        return None
    bucket_id = raw.get("id")
    if not isinstance(bucket_id, str) or not bucket_id:
        return None
    fraction = raw.get("remaining_fraction")
    if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
        return None
    return UsageBucket(
        id=bucket_id,
        name=str(raw.get("name") or bucket_id),
        window=str(raw.get("window") or ""),
        remaining_fraction=float(fraction),
        reset_time=_parse_reset_time(raw.get("reset_time")),
    )


def _parse_group(raw: object) -> Optional[UsageGroup]:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    buckets_raw = raw.get("buckets")
    if not isinstance(buckets_raw, list):
        return None
    buckets = [b for b in (_parse_bucket(item) for item in buckets_raw) if b is not None]
    if not buckets:
        return None
    return UsageGroup(name=name, buckets=buckets)


def _parse_groups(raw: object) -> List[UsageGroup]:
    if not isinstance(raw, list):
        return []
    return [g for g in (_parse_group(item) for item in raw) if g is not None]


def query_profile_usage(store, name: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> UsageResult:
    """Query one profile's quota usage or engine status. Never raises.

    Short-circuits with NO subprocess spawned at all when the profile is
    not authenticated: a doomed query is a wasted round trip, and every
    multi-profile report is already sequential (see the module
    docstring), so skipping it shortens real elapsed time, not just log
    noise.
    """
    profile = store.get(name)
    engine = profile.engine
    data_dir = store.profile_data_dir(name, engine=engine)
    state = auth_state(data_dir, store, name, engine=engine)

    if engine == "codex":
        import account
        plan = account.detect_codex_plan(data_dir)
        email = profile.email or account.detect_codex_email(data_dir)
        if state != "authenticated":
            return UsageResult(
                name=name,
                ok=False,
                engine="codex",
                email=email,
                plan=plan,
                error="not authenticated",
            )
        return UsageResult(
            name=name,
            ok=True,
            engine="codex",
            email=email,
            plan=plan or "ChatGPT Plus",
        )

    if state != "authenticated":
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error="not authenticated")

    config = store.load_config()
    binary = platforms.resolve_agy_binary(config.agy_binary)
    if binary is None:
        return UsageResult(
            name=name, ok=False, engine=engine, email=profile.email,
            error=f"agy binary not found (set {platforms.AGY_BIN_ENV} or PATH)",
        )

    try:
        overlay = isolation.build_overlay(name, data_dir, store.root)
    except (isolation.IsolationError, OSError) as exc:
        # OSError covers the overlay-build races that surface as raw
        # filesystem errors (e.g. FileExistsError when a concurrent
        # session recreates the .gemini link mid-build) instead of the
        # typed IsolationError -- same failure point, same degradation.
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error=f"overlay error: {exc}")

    env = isolation.isolated_env(
        overlay,
        extra={resolver.PROFILE_ENV: name},
        config_windows_redirect_home=bool(config.settings.get("windows_redirect_home")),
    )

    argv = [str(binary), "--print", "/usage", "--output-format", "json"]
    try:
        with keychain.launch_guard(store, name, capture=False,
                                  persist_on_exit=False):
            proc = platforms.run_with_group_kill(
                argv,
                env=env,
                timeout=timeout,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
    except subprocess.TimeoutExpired:
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error="timed out")
    except (OSError, ValueError) as exc:
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error=f"could not run agy: {exc}")

    if proc.returncode != 0:
        detail_lines = (proc.stderr or "").strip().splitlines()
        suffix = f": {detail_lines[0]}" if detail_lines else ""
        err_msg = f"agy exited {proc.returncode}{suffix}"
        ineligible = "eligibility check failed" in err_msg.lower() or "not eligible" in err_msg.lower()
        return UsageResult(
            name=name, ok=False, engine=engine, email=profile.email, error=err_msg, is_ineligible=ineligible
        )

    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error="non-JSON response")

    if not isinstance(payload, dict) or payload.get("status") != "SUCCESS":
        message = payload.get("error") if isinstance(payload, dict) else None
        err_msg = str(message) if message else "not eligible"
        ineligible = "eligibility" in err_msg.lower() or "not eligible" in err_msg.lower()
        return UsageResult(
            name=name, ok=False, engine=engine, email=profile.email, error=err_msg, is_ineligible=ineligible
        )

    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    groups_raw = data.get("groups") if isinstance(data, dict) else None
    groups = _parse_groups(groups_raw)
    if not groups:
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error="no usage data in response")

    return UsageResult(name=name, ok=True, engine=engine, email=profile.email, groups=groups)


def gather_usage_report(
    store,
    names: List[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    on_progress: Optional[ProgressCallback] = None,
) -> List[UsageResult]:
    """Query usage for every profile in ``names``, ONE AT A TIME.

    This is a CORRECTNESS requirement, not a performance choice -- see
    this module's docstring. The one shared macOS keychain slot that
    ``keychain.launch_guard`` swaps per profile would be raced by two
    concurrent queries for different profiles, each possibly reporting
    the WRONG account's quota under the WRONG credential. Do not
    parallelize this loop with a thread pool, ``asyncio``, or
    ``concurrent.futures`` -- ever.

    ``on_progress(index, total, name)``, when given, is called right
    BEFORE each profile's query starts (1-based ``index``), so a caller
    can render a "checking <name>... (i/N)" indicator while a report
    across several profiles is still in flight.

    Per-profile failure isolation: ``query_profile_usage`` itself already
    degrades every expected failure to a ``UsageResult(ok=False, error=..)``
    (timeout, agy exit code, overlay race, ...). An UNEXPECTED exception
    from one profile must degrade the same way instead of aborting the
    whole report -- one broken profile never hides the other accounts'
    data, mirroring the degradation pattern the single-query path uses.
    """
    results: List[UsageResult] = []
    total = len(names)
    for index, name in enumerate(names, start=1):
        if on_progress is not None:
            on_progress(index, total, name)
        try:
            results.append(query_profile_usage(store, name, timeout=timeout))
        except Exception as exc:  # noqa: BLE001 - degrade, never abort
            results.append(
                UsageResult(name=name, ok=False,
                            error=f"unexpected error: {exc}")
            )
    return results


def usage_color(remaining_fraction: float) -> str:
    """Threshold color for a remaining-fraction cell.

    green >= 50%, yellow 20-49%, red < 20%. The ONE place this threshold
    is defined -- both the compact table and the detailed bar view reuse
    this instead of each keeping its own copy of the cutoffs. Lives here
    (not in ``ui.py``) because the thresholds are usage-specific business
    semantics, not a generic rendering primitive.
    """
    if remaining_fraction >= 0.5:
        return "green"
    if remaining_fraction >= 0.2:
        return "yellow"
    return "red"


def format_mini_bar(remaining_fraction: float, width: int = 10) -> str:
    """Format a compact fixed-width gauge using filled and empty blocks: e.g. '███░░░░░░░'."""
    clamped = max(0.0, min(1.0, float(remaining_fraction)))
    filled = round(clamped * width)
    return "█" * filled + "░" * (width - filled)


def extract_model_summary(groups: List[UsageGroup]) -> dict:
    """Extract quota availability for standard model families ('gemini' and 'claude')."""
    summary = {
        "gemini": {"weekly": None, "five_h": None, "available": None},
        "claude": {"weekly": None, "five_h": None, "available": None},
    }
    for group in groups:
        lower_name = group.name.lower()
        key = None
        if "gemini" in lower_name:
            key = "gemini"
        elif any(k in lower_name for k in ("claude", "gpt", "3p")):
            key = "claude"
        elif summary["gemini"]["available"] is None:
            key = "gemini"
        elif summary["claude"]["available"] is None:
            key = "claude"

        if key is None:
            continue

        for bucket in group.buckets:
            win = bucket.window.lower()
            bid = bucket.id.lower()
            if "weekly" in win or "weekly" in bid:
                summary[key]["weekly"] = bucket.remaining_fraction
            elif "5h" in win or "5h" in bid:
                summary[key]["five_h"] = bucket.remaining_fraction

    for key in ("gemini", "claude"):
        w = summary[key]["weekly"]
        f = summary[key]["five_h"]
        if w is not None and f is not None:
            summary[key]["available"] = min(w, f)
        elif w is not None:
            summary[key]["available"] = w
        elif f is not None:
            summary[key]["available"] = f

    return summary


def _abbreviate_group(name: str) -> str:
    """Compact, deterministic column-header text for a group name.

    Strips a trailing "models"/"model" word (present in every group name
    seen so far, e.g. "Gemini Models") and uppercases the rest, truncating
    to 14 characters so an unusually long future group name still fits a
    table cell. Never a hand-written literal per group -- always derived
    from whatever ``group["name"]`` the live response held.
    """
    text = name.strip()
    for suffix in (" models", " model"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
            break
    text = text.upper()
    if len(text) > 14:
        text = text[:14].rstrip()
    return text


_WINDOW_ABBREVIATIONS = {
    "weekly": "WK",
    "5h": "5H",
}


def _abbreviate_window(window: str) -> str:
    text = (window or "").strip().lower()
    if not text:
        return "?"
    return _WINDOW_ABBREVIATIONS.get(text, text[:4].upper())


def column_header(group_name: str, window: str) -> str:
    """Column header text for a bucket, derived from live response data.

    Never a hardcoded literal like ``"Gemini Weekly"``: always computed
    from ``group["name"]`` + ``bucket["window"]`` so a schema change (a
    renamed or added model group) is reflected automatically.
    """
    return f"{_abbreviate_group(group_name)} {_abbreviate_window(window)}".strip()


def collect_bucket_columns(results: List[UsageResult]) -> List[BucketColumn]:
    """Union of every bucket ``id`` seen across ``results``, first-seen order.

    Most accounts expose the same 4 buckets today
    (``gemini-weekly``/``gemini-5h``/``3p-weekly``/``3p-5h``), but nothing
    here assumes exactly those 4 or those names -- a profile whose account
    exposes a different set of groups/buckets just adds/omits columns.
    """
    seen: dict = {}
    order: List[BucketColumn] = []
    for result in results:
        if not result.ok:
            continue
        for group in result.groups:
            for bucket in group.buckets:
                if bucket.id in seen:
                    continue
                seen[bucket.id] = True
                order.append(
                    BucketColumn(id=bucket.id, header=column_header(group.name, bucket.window))
                )
    return order


def bucket_by_id(result: UsageResult, bucket_id: str) -> Optional[UsageBucket]:
    """First bucket matching ``bucket_id`` across every group of ``result``."""
    for group in result.groups:
        for bucket in group.buckets:
            if bucket.id == bucket_id:
                return bucket
    return None


def format_countdown(reset_time: Optional[datetime], *, now: Optional[datetime] = None) -> str:
    """Compact human reset countdown, e.g. ``"6d 4h"``, ``"4h 42m"``, ``"12m"``.

    The ONE duration-formatting helper in the project -- both the compact
    table (which does not need it today) and the detailed view reuse it
    rather than each growing its own copy.
    """
    if reset_time is None:
        return "-"
    now = now or datetime.now(timezone.utc)
    total_seconds = int((reset_time - now).total_seconds())
    if total_seconds <= 0:
        return "now"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
