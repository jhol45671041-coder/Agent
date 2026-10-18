"""
OAuth 2.1 client for remote MCP servers that require sign-in
(e.g. the official TradingView MCP at https://mcp.tradingview.com/mcp).

Implements the MCP authorization flow end-to-end:
  1. Protected-resource discovery   (RFC 9728)  — from the 401
     WWW-Authenticate header, or {origin}/.well-known/oauth-protected-resource{path}
  2. Authorization-server metadata  (RFC 8414)  — /.well-known/oauth-authorization-server
  3. Dynamic client registration    (RFC 7591)  — public client, no secret
  4. Authorization code + PKCE S256 (RFC 7636), with the RFC 8707 `resource`
     parameter the MCP spec requires
  5. Token storage, refresh (refresh_token grant) and revocation

Verified live (Oct 2026) TradingView endpoints this flow discovers:
  resource metadata : https://mcp.tradingview.com/.well-known/oauth-protected-resource/mcp
  authorization     : https://www.tradingview.com/mcp/oauth/authorize
  token             : https://www.tradingview.com/mcp/oauth/token
  registration      : https://www.tradingview.com/mcp/oauth/register
  scopes            : mcp:read, mcp:tools   (TradingView Essential plan or above)

Tokens are stored in data/oauth_tokens.json (git-ignored). Nothing about the
user's TradingView password ever touches this app — the sign-in happens on
tradingview.com itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TOKENS_FILE = DATA_DIR / "oauth_tokens.json"
PENDING_FILE = DATA_DIR / "oauth_pending.json"
CLIENT_FILE = DATA_DIR / "oauth_client.json"

DEFAULT_TIMEOUT = 20.0


class OAuthError(Exception):
    """User-facing OAuth failure with an actionable message."""


def _friendly_network_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if any(s in low for s in ("tls", "ssl", "eof", "connecterror", "connection", "name or service", "timed out", "timeout")):
        return (
            f"Could not reach the authorization server from this app's server ({msg}). "
            "If you are using the hosted Arena preview, its sandbox network cannot reach "
            "tradingview.com — run the tracker on your own computer (see README) and the "
            "sign-in will work there."
        )
    return msg


# --------------------------------------------------------------------------- #
# small JSON file helpers
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_tokens() -> dict:
    return _read_json(TOKENS_FILE)


def clear_tokens() -> None:
    TOKENS_FILE.unlink(missing_ok=True)
    PENDING_FILE.unlink(missing_ok=True)


def token_status() -> dict:
    t = load_tokens()
    return {
        "hasToken": bool(t.get("access_token")),
        "expiresAt": t.get("expires_at"),
        "scope": t.get("scope", ""),
        "mcpUrl": t.get("mcp_url", ""),
    }


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def parse_www_authenticate(header: str | None) -> dict:
    """Parse `Bearer resource_metadata="...", scope="..."` into a dict."""
    params: dict[str, str] = {}
    if header:
        for key, val in re.findall(r'(\w+)="([^"]*)"', header):
            params[key] = val
    return params


async def _get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        resp = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthError(_friendly_network_error(exc)) from exc
    if resp.status_code >= 400:
        return None
    try:
        data = resp.json()
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


async def discover(mcp_url: str, www_authenticate: str | None = None) -> dict:
    """Return {resource, resource_metadata, authorization_server, as_metadata}."""
    parsed = urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    www = parse_www_authenticate(www_authenticate)
    candidates: list[str] = []
    if www.get("resource_metadata"):
        candidates.append(www["resource_metadata"])
    if path:
        candidates.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    candidates.append(f"{origin}/.well-known/oauth-protected-resource")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        resource_meta = None
        for url in candidates:
            resource_meta = await _get_json(client, url)
            if resource_meta and resource_meta.get("authorization_servers"):
                break
            resource_meta = None
        if not resource_meta:
            raise OAuthError(
                "Could not find OAuth protected-resource metadata for this MCP server "
                f"(tried: {', '.join(candidates)}). The server may not support OAuth sign-in."
            )

        as_base = resource_meta["authorization_servers"][0].rstrip("/")
        as_parsed = urlparse(as_base)
        as_origin = f"{as_parsed.scheme}://{as_parsed.netloc}"
        as_path = as_parsed.path.rstrip("/")
        as_candidates = [
            f"{as_origin}/.well-known/oauth-authorization-server{as_path}" if as_path
            else f"{as_origin}/.well-known/oauth-authorization-server",
            f"{as_base}/.well-known/oauth-authorization-server",
            f"{as_origin}/.well-known/openid-configuration{as_path}" if as_path
            else f"{as_origin}/.well-known/openid-configuration",
        ]
        as_meta = None
        for url in as_candidates:
            as_meta = await _get_json(client, url)
            if as_meta and as_meta.get("authorization_endpoint") and as_meta.get("token_endpoint"):
                break
            as_meta = None
        if not as_meta:
            raise OAuthError("Could not fetch authorization-server metadata (RFC 8414).")

    return {
        "resource": resource_meta.get("resource") or mcp_url,
        "resource_metadata": resource_meta,
        "authorization_server": as_base,
        "as_metadata": as_meta,
        "scope_hint": www.get("scope", ""),
    }


# --------------------------------------------------------------------------- #
# Dynamic client registration (cached per registration endpoint + redirect)
# --------------------------------------------------------------------------- #
async def _register_client(as_meta: dict, redirect_uri: str) -> dict:
    reg_endpoint = as_meta.get("registration_endpoint")
    cache = _read_json(CLIENT_FILE)
    key = f"{reg_endpoint}|{redirect_uri}"
    if cache.get(key, {}).get("client_id"):
        return cache[key]
    if not reg_endpoint:
        raise OAuthError(
            "This authorization server does not offer dynamic client registration, "
            "so this app cannot register itself automatically."
        )
    payload = {
        "client_name": "Trading Tracker",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "web",
    }
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(reg_endpoint, json=payload,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthError(_friendly_network_error(exc)) from exc
    if resp.status_code >= 400:
        raise OAuthError(f"Dynamic client registration failed (HTTP {resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if not data.get("client_id"):
        raise OAuthError("Registration response contained no client_id.")
    record = {"client_id": data["client_id"], "client_secret": data.get("client_secret"),
              "registration_endpoint": reg_endpoint, "redirect_uri": redirect_uri}
    cache[key] = record
    _write_json(CLIENT_FILE, cache)
    return record


# --------------------------------------------------------------------------- #
# Authorization flow
# --------------------------------------------------------------------------- #
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _choose_scope(disco: dict) -> str:
    if disco.get("scope_hint"):
        return disco["scope_hint"]
    supported = (disco["resource_metadata"].get("scopes_supported")
                 or disco["as_metadata"].get("scopes_supported") or [])
    preferred = [s for s in ("mcp:read", "mcp:tools") if s in supported]
    return " ".join(preferred or supported)


async def start_flow(mcp_url: str, redirect_uri: str, www_authenticate: str | None = None) -> str:
    """Discover + register + build the authorization URL to send the user to."""
    disco = await discover(mcp_url, www_authenticate)
    as_meta = disco["as_metadata"]
    client_reg = await _register_client(as_meta, redirect_uri)

    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    scope = _choose_scope(disco)

    _write_json(PENDING_FILE, {
        "state": state, "code_verifier": verifier, "redirect_uri": redirect_uri,
        "client_id": client_reg["client_id"], "client_secret": client_reg.get("client_secret"),
        "token_endpoint": as_meta["token_endpoint"],
        "revocation_endpoint": as_meta.get("revocation_endpoint"),
        "resource": disco["resource"], "mcp_url": mcp_url, "created_at": time.time(),
    })

    params = {
        "response_type": "code",
        "client_id": client_reg["client_id"],
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": disco["resource"],
    }
    if scope:
        params["scope"] = scope
    return f"{as_meta['authorization_endpoint']}?{urlencode(params)}"


async def _token_request(form: dict, token_endpoint: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(token_endpoint, data=form,
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthError(_friendly_network_error(exc)) from exc
    if resp.status_code >= 400:
        raise OAuthError(f"Token request failed (HTTP {resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if not data.get("access_token"):
        raise OAuthError("Token response contained no access_token.")
    return data


def _save_tokens(data: dict, meta: dict, previous: dict | None = None) -> dict:
    expires_in = data.get("expires_in")
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or (previous or {}).get("refresh_token"),
        "token_type": data.get("token_type", "Bearer"),
        "scope": data.get("scope", ""),
        "expires_at": time.time() + float(expires_in) if expires_in else None,
        "client_id": meta.get("client_id"), "client_secret": meta.get("client_secret"),
        "token_endpoint": meta.get("token_endpoint"),
        "revocation_endpoint": meta.get("revocation_endpoint"),
        "resource": meta.get("resource"), "mcp_url": meta.get("mcp_url"),
    }
    _write_json(TOKENS_FILE, tokens)
    return tokens


async def complete_flow(code: str, state: str) -> dict:
    pending = _read_json(PENDING_FILE)
    if not pending:
        raise OAuthError("No sign-in is in progress — start again from the tracker.")
    if state != pending.get("state"):
        raise OAuthError("State mismatch — possible CSRF. Start the sign-in again.")
    if time.time() - float(pending.get("created_at", 0)) > 1800:
        raise OAuthError("This sign-in link expired — start again from the tracker.")
    form = {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": pending["redirect_uri"], "client_id": pending["client_id"],
        "code_verifier": pending["code_verifier"], "resource": pending.get("resource") or "",
    }
    if pending.get("client_secret"):
        form["client_secret"] = pending["client_secret"]
    data = await _token_request({k: v for k, v in form.items() if v}, pending["token_endpoint"])
    tokens = _save_tokens(data, pending)
    PENDING_FILE.unlink(missing_ok=True)
    return tokens


async def get_access_token() -> str | None:
    """Current access token, silently refreshed if expired (when possible)."""
    tokens = load_tokens()
    token = tokens.get("access_token")
    if not token:
        return None
    expires_at = tokens.get("expires_at")
    if expires_at and expires_at - time.time() < 60 and tokens.get("refresh_token"):
        form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                "client_id": tokens.get("client_id") or "", "resource": tokens.get("resource") or ""}
        if tokens.get("client_secret"):
            form["client_secret"] = tokens["client_secret"]
        try:
            data = await _token_request({k: v for k, v in form.items() if v}, tokens["token_endpoint"])
            tokens = _save_tokens(data, tokens, previous=tokens)
            token = tokens["access_token"]
        except OAuthError:
            clear_tokens()  # refresh failed -> user must sign in again
            return None
    return token


async def revoke() -> None:
    tokens = load_tokens()
    endpoint = tokens.get("revocation_endpoint")
    if endpoint and tokens.get("access_token"):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(endpoint, data={
                    "token": tokens["access_token"], "client_id": tokens.get("client_id") or "",
                    **({"client_secret": tokens["client_secret"]} if tokens.get("client_secret") else {}),
                })
        except httpx.HTTPError:
            pass
    clear_tokens()
