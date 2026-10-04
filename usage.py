"""Aggregate quota usage across profiles through each engine's usage source.

Mechanism (validated live against a real agy 1.2.7 binary): running
``agy --print "/usage" --output-format json`` is a stateless, read-only
quota query. It needs no TTY and no interactive prompt, and it returns
clean structured JSON on stdout under ``command.data.groups`` -- a list of
model groups, each holding one or more usage buckets (``weekly``/``5h``
windows) with a ``remaining_fraction`` (0..1) and a UTC ``reset_time``.

For agy's response, never hardcode group/bucket NAMES ("Gemini"/"Claude")
as string literals: everything is read dynamically from
``group["name"]``/``bucket["window"]``/``bucket["id"]`` so a future agy
release that adds or renames a model group still renders correctly.

Concurrency-safety note (also validated live): a single ``/usage`` query
is safe to run concurrently with an ALREADY-RUNNING agy session for the
SAME profile -- it is a stateless read, not a conversation turn -- which
is why ``query_profile_usage`` deliberately does NOT take the session
lock (``locks.try_lock``). ``usage`` must keep working while a profile is
busy; this is the one documented exception alongside ``-f/--force`` (see
AGENTS.md's lock-exceptions bullet).

Codex usage requests only use access tokens already present in the
profile data: this read-only path never exchanges refresh tokens or
rewrites ``auth.json``; missing and expired access tokens return explicit
errors so an inspection cannot rotate credentials behind a running
session. Grok usage self-heals an expired session instead: a 401 from
the billing endpoint triggers one OIDC ``refresh_token`` grant, the
billing query is retried with the fresh access token, and the grant is
persisted through ``account.update_grok_tokens`` — compare-and-swap on
the token seen at inspection time, so a live session's concurrent
rotation always wins and this writer silently stands down. When the
refresh itself fails the error names the re-login command.

Antigravity queries run keychain-free through ``usage_agy``: the
profile's own credential is staged into a throwaway ``HOME`` whose
``SSH_TTY`` flag makes agy read the on-disk token file instead of the
shared macOS keychain slot, so a query never swaps credentials, never
takes ``swap.lock``, and coexists with live sessions of any profile. Any
OAuth refresh agy performs lands inside the staging directory and is
discarded with it.

``gather_usage_report`` below iterates profiles strictly one at a time.
With per-profile scoped staging there is no shared-slot race left, but
the sequential loop keeps report output deterministic and one engine
subprocess on the machine at a time. Do not "optimize" this loop.

The meaning of ``UsageResult.ok`` is engine-specific. The agy subprocess
path reports failures as ``ok=False``. For Codex and Grok, missing
credentials, HTTP 401, invalid responses, and absent usage data from the
required quota endpoint return ``ok=False``. With locally recognized
credentials, a transport failure or an HTTP error other than 401 from that
endpoint returns ``ok=True`` with no groups and an explicit ``error``: the
account is recognized, but quota data is unavailable. A successful quota
response returns ``ok=True`` with groups; API-key authentication can also
return ``ok=True`` without groups or an error. Grok's settings endpoint is
an optional best-effort lookup for the displayed plan; its failure is
ignored and does not invalidate the billing status. ``query_profile_usage``
never raises, so one broken or ineligible profile can never abort a
multi-profile report.
"""
from __future__ import annotations

import json
import math
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Literal, Optional, Tuple

import platforms
import usage_agy
from account import CHATGPT_PLAN_NAMES, auth_state, normalize_email, _has_credential
from models import Profile

DEFAULT_TIMEOUT_S = 20

OPENAI_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
GROK_TOKEN_AUTH_HEADER = "xai-grok-cli"
GROK_DEFAULT_OIDC_ISSUER = "https://auth.x.ai"
GROK_DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"

ProgressCallback = Callable[[int, int, str], None]
AuthenticationState = Literal["authenticated", "unauthenticated"]


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
    source: Optional[str] = None
    observed_at: Optional[datetime] = None
    quality: Optional[str] = None
    identity_verified: Optional[bool] = None
    authentication_state: Optional[AuthenticationState] = None


class UsageResponseError(ValueError):
    """An external usage endpoint returned data that could not be parsed."""


def _is_ineligible_message(text: object) -> bool:
    """True when ``text`` looks like an agy eligibility rejection.

    Single source of truth so the ``is_ineligible`` flag stays consistent
    across the subprocess-stderr path and the JSON-error path.
    """
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return "eligibility" in lowered or "not eligible" in lowered


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def parse_iso_utc(raw: object, *, require_utc: bool = False) -> Optional[datetime]:
    """Decode an ISO-8601 UTC timestamp or unix epoch into a ``datetime``.

    Single source of truth for every timestamp Agydra reads: quota reset
    times, snapshot ``observed_at`` stamps and Claude usage windows all
    go through here, so the edge cases are handled exactly once.

    ``datetime.fromisoformat`` only accepts the trailing ``Z`` shorthand
    starting with Python 3.11; this project's floor is 3.9, so a trailing
    ``Z``/``z`` is normalized to ``+00:00`` by hand before parsing. Numeric
    timestamps (seconds or milliseconds) are also supported. A naive
    string is read as UTC unless ``require_utc`` rejects it outright.
    Malformed/missing values degrade to ``None`` rather than raising, so a
    caller reports "unknown" instead of failing the whole report.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            ts = float(raw)
            if not math.isfinite(ts):
                return None
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if require_utc:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    elif require_utc:
        try:
            if parsed.utcoffset() != timedelta(0):
                return None
        except (OverflowError, ValueError):
            return None
    return parsed


def _parse_bucket(raw: object) -> Optional[UsageBucket]:
    if not isinstance(raw, dict):
        return None
    bucket_id = raw.get("id")
    if not isinstance(bucket_id, str) or not bucket_id:
        return None
    fraction = raw.get("remaining_fraction")
    if not _is_finite_number(fraction):
        return None
    clamped = max(0.0, min(1.0, float(fraction)))
    return UsageBucket(
        id=bucket_id,
        name=str(raw.get("name") or bucket_id),
        window=str(raw.get("window") or ""),
        remaining_fraction=clamped,
        reset_time=parse_iso_utc(raw.get("reset_time")),
    )


def _parse_window_bucket(
    raw_window: object,
    *,
    bucket_id: str,
    name: str,
    window: str,
    used_key: str = "used_percent",
    reset_key: str = "reset_at",
    strict_percentage: bool = False,
    strict_reset_utc: bool = False,
) -> Optional[UsageBucket]:
    if not isinstance(raw_window, dict):
        return None
    used = raw_window.get(used_key)
    if not _is_finite_number(used):
        return None
    used_percent = float(used)
    if strict_percentage and not 0.0 <= used_percent <= 100.0:
        return None
    rem = max(0.0, min(1.0, (100.0 - used_percent) / 100.0))
    raw_reset = raw_window.get(reset_key)
    reset_dt = parse_iso_utc(raw_reset, require_utc=strict_reset_utc)
    if strict_reset_utc and raw_reset is not None and reset_dt is None:
        return None
    return UsageBucket(
        id=bucket_id,
        name=name,
        window=window,
        remaining_fraction=rem,
        reset_time=reset_dt,
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


def _get_profile_readonly(store, name: str) -> Profile:
    return store.get_readonly(name)


def _has_pending_profile_rename(store) -> bool:
    return store.has_pending_rename()


def _close_http_error(exc: BaseException) -> None:
    """Close an HTTP error body. urllib leaves that to the caller, and
    ``HTTPError`` warns at garbage collection when it stays open.
    """
    if isinstance(exc, urllib.error.HTTPError):
        try:
            exc.close()
        except Exception:
            pass


def _read_json_object_response(response, source: str) -> dict:
    try:
        payload = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise UsageResponseError(f"{source} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise UsageResponseError(f"{source} returned a non-object JSON response")
    return payload


def _post_token_refresh(
    url: str,
    body: bytes,
    content_type: str,
    *,
    user_agent: str = "Mozilla/5.0",
    timeout: float = 10.0,
) -> Optional[dict]:
    """Execute a token refresh POST request and parse JSON tokens response."""
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": content_type,
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _read_json_object_response(resp, "token refresh endpoint")
            if isinstance(data, dict) and (data.get("access_token") or data.get("key")):
                return data
    except Exception as exc:
        _close_http_error(exc)
        return None
    return None


def refresh_grok_tokens(
    refresh_token: str,
    *,
    client_id: Optional[str] = None,
    issuer: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[dict]:
    """Call xAI Grok OIDC refresh endpoint to exchange refresh_token for fresh tokens."""
    cid = client_id or GROK_DEFAULT_CLIENT_ID
    iss = (issuer or GROK_DEFAULT_OIDC_ISSUER).rstrip("/")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": cid,
        "refresh_token": refresh_token,
    }).encode("utf-8")
    return _post_token_refresh(
        f"{iss}/oauth2/token",
        body,
        "application/x-www-form-urlencoded",
        user_agent="xai-grok-cli",
        timeout=timeout,
    )


def fetch_codex_usage_payload(
    access_token: str,
    *,
    account_id: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[dict]:
    """Query OpenAI internal wham/usage endpoint using a Bearer access_token."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    req = urllib.request.Request(
        OPENAI_USAGE_URL,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _read_json_object_response(resp, "OpenAI usage endpoint")


def parse_codex_usage_payload(
    payload: dict,
) -> Tuple[List[UsageGroup], Optional[str], Optional[str]]:
    """Parse Codex backend-api/wham/usage JSON into UsageGroup and metadata."""
    if not isinstance(payload, dict):
        return [], None, None

    detected_email = normalize_email(payload.get("email"))
    raw_plan = payload.get("plan_type")
    detected_plan = None
    if isinstance(raw_plan, str) and raw_plan:
        detected_plan = CHATGPT_PLAN_NAMES.get(raw_plan.lower(), f"ChatGPT {raw_plan.capitalize()}")

    credits_info = payload.get("credits")
    if isinstance(credits_info, dict) and detected_plan:
        balance = credits_info.get("balance")
        if _is_finite_number(balance):
            detected_plan = f"{detected_plan} (${float(balance):.2f})"
        elif isinstance(balance, str) and balance.strip():
            try:
                bal_f = float(balance.strip())
                if math.isfinite(bal_f):
                    detected_plan = f"{detected_plan} (${bal_f:.2f})"
            except ValueError:
                pass

    buckets: List[UsageBucket] = []

    rate_limit = payload.get("rate_limit")
    if isinstance(rate_limit, dict):
        b_5h = _parse_window_bucket(
            rate_limit.get("primary_window"),
            bucket_id="codex-5h",
            name="5 Hours",
            window="5h",
        )
        if b_5h:
            buckets.append(b_5h)

        b_wk = _parse_window_bucket(
            rate_limit.get("secondary_window"),
            bucket_id="codex-weekly",
            name="Weekly",
            window="weekly",
        )
        if b_wk:
            buckets.append(b_wk)

    add_limits = payload.get("additional_rate_limits")
    if isinstance(add_limits, list):
        for item in add_limits:
            if not isinstance(item, dict):
                continue
            limit_name = item.get("limit_name") or item.get("metered_feature")
            if not isinstance(limit_name, str) or not limit_name.strip():
                continue
            clean_name = limit_name.strip()
            slug = clean_name.lower().replace(" ", "-")
            item_rl = item.get("rate_limit")
            if isinstance(item_rl, dict):
                b5 = _parse_window_bucket(
                    item_rl.get("primary_window"),
                    bucket_id=f"codex-{slug}-5h",
                    name=f"{clean_name} 5h",
                    window="5h",
                )
                if b5:
                    buckets.append(b5)
                bw = _parse_window_bucket(
                    item_rl.get("secondary_window"),
                    bucket_id=f"codex-{slug}-weekly",
                    name=f"{clean_name} Weekly",
                    window="weekly",
                )
                if bw:
                    buckets.append(bw)

    spend_limit = (
        payload.get("individual_limit")
        or (rate_limit.get("individual_limit") if isinstance(rate_limit, dict) else None)
        or (payload.get("spend_control", {}).get("individual_limit") if isinstance(payload.get("spend_control"), dict) else None)
    )
    if isinstance(spend_limit, dict):
        rem_pct = spend_limit.get("remaining_percent")
        if not _is_finite_number(rem_pct):
            used = spend_limit.get("used")
            limit = spend_limit.get("limit")
            if (
                _is_finite_number(used)
                and _is_finite_number(limit)
                and limit > 0
            ):
                rem_pct = max(0.0, min(100.0, (1.0 - (float(used) / float(limit))) * 100.0))
        if _is_finite_number(rem_pct):
            rem_fraction = max(0.0, min(1.0, float(rem_pct) / 100.0))
            reset_raw = spend_limit.get("reset_at") or spend_limit.get("resets_at")
            reset_dt = parse_iso_utc(reset_raw)
            buckets.append(
                UsageBucket(
                    id="codex-spend",
                    name="Spend Limit",
                    window="monthly",
                    remaining_fraction=rem_fraction,
                    reset_time=reset_dt,
                )
            )

    if not buckets:
        return [], detected_plan, detected_email

    return [UsageGroup(name="OpenAI Codex", buckets=buckets)], detected_plan, detected_email


def query_codex_usage(
    data_dir: Path,
    name: str,
    *,
    email: Optional[str] = None,
    plan: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> UsageResult:
    """Query OpenAI Codex usage via backend-api/wham/usage. Never raises."""
    import account

    auth_info = account.inspect_codex_auth(data_dir)
    detected_plan = plan or account.detect_codex_plan(data_dir)
    detected_email = normalize_email(email) or account.detect_codex_email(data_dir)

    if auth_info is None:
        return UsageResult(
            name=name,
            ok=False,
            engine="codex",
            email=detected_email,
            plan=detected_plan,
            error="not authenticated",
            authentication_state="unauthenticated",
        )

    if auth_info.get("auth_type") == "api_key":
        return UsageResult(
            name=name,
            ok=True,
            engine="codex",
            email=detected_email,
            plan=detected_plan or "OpenAI API Key",
            authentication_state="authenticated",
        )

    access_token = auth_info.get("access_token")
    account_id = auth_info.get("account_id")

    if not access_token:
        return UsageResult(
            name=name,
            ok=False,
            engine="codex",
            email=detected_email,
            plan=detected_plan,
            error="missing access token",
            authentication_state="unauthenticated",
        )

    payload = None
    try:
        payload = fetch_codex_usage_payload(access_token, account_id=account_id, timeout=timeout)
    except urllib.error.HTTPError as exc:
        _close_http_error(exc)
        if exc.code == 401:
            return UsageResult(
                name=name,
                ok=False,
                engine="codex",
                email=detected_email,
                plan=detected_plan,
                error="session expired (401)",
                authentication_state="unauthenticated",
            )
        else:
            return UsageResult(
                name=name,
                ok=True,
                engine="codex",
                email=detected_email,
                plan=detected_plan or "ChatGPT Plus",
                error=f"usage unavailable (HTTP {exc.code})",
                authentication_state="authenticated",
            )
    except UsageResponseError as exc:
        return UsageResult(
            name=name,
            ok=False,
            engine="codex",
            email=detected_email,
            plan=detected_plan,
            error=f"invalid usage response ({exc})",
            authentication_state="authenticated",
        )
    except Exception as exc:
        _close_http_error(exc)
        return UsageResult(
            name=name,
            ok=True,
            engine="codex",
            email=detected_email,
            plan=detected_plan or "ChatGPT Plus",
            error=f"offline ({exc})",
            authentication_state="authenticated",
        )

    if not payload:
        return UsageResult(
            name=name,
            ok=False,
            engine="codex",
            email=detected_email,
            plan=detected_plan or "ChatGPT Plus",
            error="no usage data in response",
            authentication_state="authenticated",
        )

    groups, api_plan, api_email = parse_codex_usage_payload(payload)
    final_plan = api_plan or detected_plan or "ChatGPT Plus"
    final_email = api_email or detected_email

    if not groups:
        return UsageResult(
            name=name,
            ok=False,
            engine="codex",
            email=final_email,
            plan=final_plan,
            error="no usage data in response",
            authentication_state="authenticated",
        )

    return UsageResult(
        name=name,
        ok=True,
        engine="codex",
        email=final_email,
        plan=final_plan,
        groups=groups,
        authentication_state="authenticated",
    )


def fetch_grok_billing_payload(token: str, *, timeout: float = 10.0) -> Optional[dict]:
    """Query xAI Grok cli-chat-proxy billing endpoint using a Bearer token."""
    req = urllib.request.Request(
        GROK_BILLING_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "x-xai-token-auth": GROK_TOKEN_AUTH_HEADER,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _read_json_object_response(resp, "xAI billing endpoint")


def fetch_grok_settings_payload(token: str, *, timeout: float = 4.0) -> Optional[dict]:
    """Query xAI Grok cli-chat-proxy settings endpoint for subscription tier display."""
    req = urllib.request.Request(
        GROK_SETTINGS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "x-xai-token-auth": GROK_TOKEN_AUTH_HEADER,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read_json_object_response(resp, "xAI settings endpoint")
    except Exception as exc:
        _close_http_error(exc)
        return None


def parse_grok_billing_payload(
    payload: dict,
) -> Tuple[List[UsageGroup], Optional[datetime]]:
    """Parse Grok cli-chat-proxy/v1/billing JSON into UsageGroup and reset time."""
    if not isinstance(payload, dict):
        return [], None

    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        return [], None

    used_pct = cfg.get("creditUsagePercent")
    if not _is_finite_number(used_pct):
        ondemand_used = (
            cfg.get("onDemandUsed", {}).get("val")
            if isinstance(cfg.get("onDemandUsed"), dict)
            else None
        )
        ondemand_cap = (
            cfg.get("onDemandCap", {}).get("val")
            if isinstance(cfg.get("onDemandCap"), dict)
            else None
        )
        if (
            _is_finite_number(ondemand_used)
            and _is_finite_number(ondemand_cap)
            and ondemand_cap > 0
        ):
            used_pct = (float(ondemand_used) / float(ondemand_cap)) * 100.0
        else:
            used_pct = None

    if used_pct is None:
        return [], None

    rem = max(0.0, min(1.0, (100.0 - float(used_pct)) / 100.0))

    reset_raw = None
    curr_period = cfg.get("currentPeriod")
    if isinstance(curr_period, dict):
        reset_raw = curr_period.get("end")
    if not reset_raw:
        reset_raw = cfg.get("billingPeriodEnd")

    reset_dt = parse_iso_utc(reset_raw) if reset_raw else None

    bucket = UsageBucket(
        id="grok-weekly",
        name="Weekly",
        window="weekly",
        remaining_fraction=rem,
        reset_time=reset_dt,
    )
    buckets = [bucket]

    prod_usage = cfg.get("productUsage")
    if isinstance(prod_usage, list):
        for item in prod_usage:
            if not isinstance(item, dict):
                continue
            prod_name = item.get("product")
            if not isinstance(prod_name, str) or not prod_name.strip():
                continue
            clean_prod = prod_name.strip()
            pct = item.get("usagePercent")
            if _is_finite_number(pct):
                prod_rem = max(0.0, min(1.0, (100.0 - float(pct)) / 100.0))
                slug = clean_prod.lower().replace(" ", "-")
                buckets.append(
                    UsageBucket(
                        id=f"grok-{slug}",
                        name=clean_prod,
                        window="weekly",
                        remaining_fraction=prod_rem,
                        reset_time=reset_dt,
                    )
                )

    return [UsageGroup(name="xAI Grok", buckets=buckets)], reset_dt


def _refresh_grok_safely(auth_info: dict, timeout: float) -> Optional[dict]:
    """Call the xAI OIDC refresh endpoint; swallow network/HTTP failures.

    Returns the parsed token grant on success, ``None`` otherwise. Usage
    callers treat any failure here as ``needs re-login`` rather than
    surfacing transport noise to the user inspecting quotas.
    """
    refresh_token = auth_info.get("refresh_token")
    if not refresh_token:
        return None
    try:
        return refresh_grok_tokens(
            refresh_token,
            client_id=auth_info.get("oidc_client_id"),
            issuer=auth_info.get("oidc_issuer"),
            timeout=timeout,
        )
    except Exception:
        return None


GROK_PLANS_WITHOUT_QUOTA = frozenset({"x premium"})
PLAN_WITHOUT_QUOTA = "plan without usage quota"


def _grok_empty_usage_result(
    name: str, email: Optional[str], plan: Optional[str]
) -> "UsageResult":
    """Result for a Grok billing response with no quota windows.

    Plans known not to expose quota data (``GROK_PLANS_WITHOUT_QUOTA``, exact
    match) are a healthy, expected state, not a failed query.
    """
    metered = (plan or "").strip().lower() not in GROK_PLANS_WITHOUT_QUOTA
    return UsageResult(
        name=name,
        ok=not metered,
        engine="grok",
        email=email,
        plan=plan or "Grok (xAI)",
        error="no usage data in response" if metered else PLAN_WITHOUT_QUOTA,
        authentication_state="authenticated",
    )


def query_grok_usage(
    data_dir: Path,
    name: str,
    *,
    email: Optional[str] = None,
    plan: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> UsageResult:
    """Query xAI Grok usage and billing via cli-chat-proxy. Never raises."""
    import account

    auth_info = account.inspect_grok_auth(data_dir)
    detected_plan = plan or account.detect_grok_plan(data_dir)
    detected_email = normalize_email(email) or account.detect_grok_email(data_dir)

    if auth_info is None:
        return UsageResult(
            name=name,
            ok=False,
            engine="grok",
            email=detected_email,
            plan=detected_plan,
            error="not authenticated",
            authentication_state="unauthenticated",
        )

    if auth_info.get("auth_type") == "api_key":
        return UsageResult(
            name=name,
            ok=True,
            engine="grok",
            email=detected_email,
            plan=detected_plan or "xAI API Key",
            authentication_state="authenticated",
        )

    token = auth_info.get("token") or auth_info.get("key")
    if not token:
        return UsageResult(
            name=name,
            ok=False,
            engine="grok",
            email=detected_email,
            plan=detected_plan,
            error="missing access token",
            authentication_state="unauthenticated",
        )

    billing_payload = None
    used_token = token
    used_previous = auth_info.get("key") if auth_info else None
    for attempt in (0, 1):
        try:
            billing_payload = fetch_grok_billing_payload(used_token, timeout=timeout)
            break
        except urllib.error.HTTPError as exc:
            _close_http_error(exc)
            if exc.code != 401:
                return UsageResult(
                    name=name,
                    ok=True,
                    engine="grok",
                    email=detected_email,
                    plan=detected_plan or "Grok (xAI)",
                    error=f"usage unavailable (HTTP {exc.code})",
                    authentication_state="authenticated",
                )
            if attempt == 0 and (auth_info or {}).get("refresh_token"):
                grant = _refresh_grok_safely(auth_info, timeout)
                if grant and _has_credential(grant.get("access_token")):
                    new_access = grant["access_token"]
                    new_refresh = (
                        grant.get("refresh_token")
                        if _has_credential(grant.get("refresh_token"))
                        else None
                    )
                    try:
                        account.update_grok_tokens(
                            data_dir,
                            access_token=new_access,
                            refresh_token=new_refresh,
                            previous_access_token=used_previous,
                        )
                    except Exception:
                        pass
                    used_token = new_access
                    used_previous = new_access
                    continue
            return UsageResult(
                name=name,
                ok=False,
                engine="grok",
                email=detected_email,
                plan=detected_plan,
                error=(
                    "session expired (401); re-login required: agydra login "
                    + name
                ),
                authentication_state="unauthenticated",
            )
        except UsageResponseError as exc:
            return UsageResult(
                name=name,
                ok=False,
                engine="grok",
                email=detected_email,
                plan=detected_plan,
                error=f"invalid usage response ({exc})",
                authentication_state="authenticated",
            )
        except Exception as exc:
            _close_http_error(exc)
            return UsageResult(
                name=name,
                ok=True,
                engine="grok",
                email=detected_email,
                plan=detected_plan or "Grok (xAI)",
                error=f"offline ({exc})",
                authentication_state="authenticated",
            )

    final_plan = detected_plan
    settings_payload = fetch_grok_settings_payload(used_token, timeout=min(timeout, 4.0))
    if isinstance(settings_payload, dict):
        tier_display = settings_payload.get("subscription_tier_display")
        if isinstance(tier_display, str) and tier_display:
            final_plan = tier_display

    groups: List[UsageGroup] = []
    if billing_payload:
        groups, _reset_dt = parse_grok_billing_payload(billing_payload)
    if not groups:
        return _grok_empty_usage_result(name, detected_email, final_plan)
    return UsageResult(
        name=name,
        ok=True,
        engine="grok",
        email=detected_email,
        plan=final_plan or "Grok (xAI)",
        groups=groups,
        authentication_state="authenticated",
    )


def query_profile_usage(store, name: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> UsageResult:
    """Query one profile's quota usage or engine status. Never raises.

    Short-circuits with NO subprocess spawned at all when the profile is
    not authenticated: a doomed query is a wasted round trip, and every
    multi-profile report is already sequential (see the module
    docstring), so skipping it shortens real elapsed time, not just log
    noise.
    """
    if _has_pending_profile_rename(store):
        try:
            profile_metadata = _get_profile_readonly(store, name)
            engine = profile_metadata.engine if profile_metadata else None
        except Exception:
            engine = None
        if engine == "claude":
            return UsageResult(
                name=name,
                ok=True,
                engine="claude",
                source="claude_status_line",
                quality="unknown",
                identity_verified=False,
                error="profile rename recovery pending",
            )
        return UsageResult(
            name=name,
            ok=False,
            engine=engine,
            error="profile rename recovery pending",
            quality="unknown",
        )

    try:
        profile_metadata = _get_profile_readonly(store, name)
    except Exception:
        profile_metadata = None

    if profile_metadata is None:
        return UsageResult(
            name=name,
            ok=False,
            error="profile metadata unavailable",
            quality="unknown",
        )

    if profile_metadata.engine == "claude":
        import claude_usage

        return claude_usage.query_claude_usage_live(
            store,
            name,
            profile=profile_metadata,
        )

    profile = profile_metadata
    engine = profile.engine
    data_dir = store.profile_data_dir(name, engine=engine)

    if engine == "codex":
        return query_codex_usage(
            data_dir,
            name,
            email=profile.email,
            timeout=timeout,
        )

    if engine == "grok":
        return query_grok_usage(
            data_dir,
            name,
            email=profile.email,
            timeout=timeout,
        )

    if engine != "agy":
        return UsageResult(
            name=name,
            ok=False,
            engine=engine,
            email=profile.email,
            error=f"unsupported usage engine: {engine}",
        )

    state = auth_state(data_dir, store, name, engine=engine)

    if state != "authenticated":
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error="not authenticated")

    config = store.load_config()
    binary = platforms.resolve_agy_binary(config.agy_binary)
    if binary is None:
        return UsageResult(
            name=name, ok=False, engine=engine, email=profile.email,
            error=f"agy binary not found (set {platforms.AGY_BIN_ENV} or PATH)",
        )

    token_bytes = usage_agy.scoped_token_bytes(store, name, profile, data_dir)
    if token_bytes is None:
        return UsageResult(
            name=name, ok=False, engine=engine, email=profile.email,
            error="credential not found or identity unverified in profile data/keychain backup (launch once or re-login to refresh)",
        )

    try:
        proc = usage_agy.run_scoped_usage_query(
            store, name, binary, token_bytes,
            timeout=timeout, config=config,
        )
    except subprocess.TimeoutExpired:
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error="timed out")
    except (OSError, ValueError) as exc:
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error=f"could not run agy: {exc}")

    if proc.returncode != 0:
        detail_lines = (proc.stderr or "").strip().splitlines()
        suffix = f": {detail_lines[0]}" if detail_lines else ""
        err_msg = f"agy exited {proc.returncode}{suffix}"
        return UsageResult(
            name=name, ok=False, engine=engine, email=profile.email,
            error=err_msg, is_ineligible=_is_ineligible_message(err_msg),
        )

    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError, RecursionError):
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error="non-JSON response")

    if not isinstance(payload, dict) or payload.get("status") != "SUCCESS":
        message = payload.get("error") if isinstance(payload, dict) else None
        err_msg = str(message) if message else "not eligible"
        return UsageResult(
            name=name, ok=False, engine=engine, email=profile.email,
            error=err_msg, is_ineligible=_is_ineligible_message(err_msg),
        )

    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    groups_raw = data.get("groups") if isinstance(data, dict) else None
    groups = _parse_groups(groups_raw)
    if not groups:
        return UsageResult(name=name, ok=False, engine=engine, email=profile.email, error="no usage data in response")

    return UsageResult(name=name, ok=True, engine=engine, email=profile.email, groups=groups, source="cli")


def gather_usage_report(
    store,
    names: List[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    on_progress: Optional[ProgressCallback] = None,
) -> List[UsageResult]:
    """Query usage for every profile in ``names``, ONE AT A TIME.

    Determinism, not a shared-resource race -- see this module's
    docstring: each Antigravity query stages its own throwaway HOME via
    ``usage_agy``, so there is no shared keychain slot left to race, but
    the sequential loop keeps report output stable and one engine
    subprocess on the machine at a time. Do not parallelize this loop
    with a thread pool, ``asyncio``, or ``concurrent.futures``.

    ``on_progress(index, total, name)``, when given, is called right
    BEFORE each profile's query starts (1-based ``index``), so a caller
    can render a "checking <name>... (i/N)" indicator while a report
    across several profiles is still in flight.

    Per-profile failure isolation: ``query_profile_usage`` itself already
    degrades every expected failure to a ``UsageResult(ok=False, error=..)``
    (timeout, agy exit code, missing credential, ...). An UNEXPECTED
    exception from one profile must degrade the same way instead of
    aborting the whole report -- one broken profile never hides the
    other accounts' data, mirroring the degradation pattern the
    single-query path uses.
    """
    results: List[UsageResult] = []
    total = len(names)
    for index, name in enumerate(names, start=1):
        if on_progress is not None:
            on_progress(index, total, name)
        try:
            results.append(query_profile_usage(store, name, timeout=timeout))
        except Exception as exc:
            known: dict = {}
            try:
                known["engine"] = store.get(name).engine
            except Exception:
                pass
            results.append(
                UsageResult(name=name, ok=False,
                            error=f"unexpected error: {exc}", **known)
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
    if width <= 0:
        return ""
    if not _is_finite_number(remaining_fraction):
        return "░" * width
    clamped = max(0.0, min(1.0, float(remaining_fraction)))
    filled = round(clamped * width)
    return "█" * filled + "░" * (width - filled)


def extract_model_summary(groups: List[UsageGroup]) -> dict:
    """Extract quota availability for standard model families ('gemini', 'claude', 'codex', 'grok')."""
    summary = {
        "gemini": {"weekly": None, "five_h": None, "available": None, "reset_time": None},
        "claude": {"weekly": None, "five_h": None, "available": None, "reset_time": None},
        "claude_code": {"weekly": None, "five_h": None, "available": None, "reset_time": None},
        "codex": {"weekly": None, "five_h": None, "available": None, "reset_time": None},
        "grok": {"weekly": None, "five_h": None, "available": None, "reset_time": None},
    }
    for group in groups:
        lower_name = group.name.lower()
        key = None
        if "gemini" in lower_name:
            key = "gemini"
        elif "claude code" in lower_name:
            key = "claude_code"
        elif any(k in lower_name for k in ("claude", "gpt", "3p")):
            key = "claude"
        elif any(k in lower_name for k in ("codex", "openai")):
            key = "codex"
        elif any(k in lower_name for k in ("grok", "xai")):
            key = "grok"
        elif summary["gemini"]["weekly"] is None and summary["gemini"]["five_h"] is None:
            key = "gemini"
        elif summary["claude"]["weekly"] is None and summary["claude"]["five_h"] is None:
            key = "claude"

        if key is None:
            continue

        for bucket in group.buckets:
            win = bucket.window.lower()
            bid = bucket.id.lower()
            if "weekly" in win or "weekly" in bid:
                summary[key]["weekly"] = bucket.remaining_fraction
                summary[key]["weekly_reset"] = bucket.reset_time
            elif "5h" in win or "5h" in bid:
                summary[key]["five_h"] = bucket.remaining_fraction
                summary[key]["five_h_reset"] = bucket.reset_time
            elif summary[key]["weekly"] is None:
                summary[key]["weekly"] = bucket.remaining_fraction
                summary[key]["weekly_reset"] = bucket.reset_time

    for key in ("gemini", "claude", "claude_code", "codex", "grok"):
        w = summary[key]["weekly"]
        f = summary[key]["five_h"]
        w_reset = summary[key].get("weekly_reset")
        f_reset = summary[key].get("five_h_reset")
        if w is not None and f is not None:
            if f <= w:
                summary[key]["available"] = f
                summary[key]["reset_time"] = f_reset
            else:
                summary[key]["available"] = w
                summary[key]["reset_time"] = w_reset
        elif w is not None:
            summary[key]["available"] = w
            summary[key]["reset_time"] = w_reset
        elif f is not None:
            summary[key]["available"] = f
            summary[key]["reset_time"] = f_reset

    return summary


def format_countdown(reset_time: Optional[datetime], *, now: Optional[datetime] = None) -> str:
    """Compact human reset countdown, e.g. ``"6d 4h"``, ``"4h 42m"``, ``"12m"``.

    The ONE duration-formatting helper in the project -- both the compact
    table (which does not need it today) and the detailed view reuse it
    rather than each growing its own copy.
    """
    if reset_time is None:
        return "-"
    if reset_time.tzinfo is None:
        reset_time = reset_time.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
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
