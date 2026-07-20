"""
agent_remote_client.py
======================

Lightweight HTTPS client for ``agent_remote_server``. Provides a
``RemoteAIAgent`` class that mirrors the constructor signature and
``run_conversation`` method shape of the in-process ``AIAgent``
class, so ``api.routes.py`` keeps calling ``require_ai_agent_class()``
exactly as before — the swap to a remote backend is a runtime choice
selected by env ``HERMES_AGENT_SERVER_TOKEN`` + ``AGENT_REMOTE_URL``.

Why this exists
---------------
nesquena/hermes-webui loads AIAgent via ``from run_agent import
AIAgent`` (in-process import). When the webui is moved to a remote
host without that module, the import fails with "AIAgent not
available". This client proxies every AIAgent call through HTTPS to
``agent_remote_server`` which lives on the host that *does* have
``run_agent``.

Scope
-----
This client implements the same call surface the webui uses (5 call
sites; ``grep -n 'AIAgent(' api/routes.py``):

- ``__init__``: capture kwargs for later use
- ``run_conversation(**kwargs)``
- ``chat(message, stream_callback=None)``
- ``context_compressor.compress(...)``

Anything else on AIAgent (interact, run_tools, etc.) is intentionally
not proxied because the webui doesn't call it.

Auth
----
``HERMES_AGENT_SERVER_TOKEN`` env var holds the bearer token. The
remote server validates with constant-time compare. ``verify=False``
disables TLS cert verification because we control both sides and the
self-signed cert won't match any expected hostname.

Failure modes
-------------
If the remote server is unreachable, every call returns
``AIAgentProxyError`` with the underlying error captured. Callers in
api.routes.py already wrap agent calls in try/except that surfaces
this as an HTTP error to the browser.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

import urllib.error
import urllib.request

LOG = logging.getLogger("agent_remote_client")

DEFAULT_TIMEOUT = float(os.environ.get("AGENT_REMOTE_TIMEOUT", "600"))
CERT_BYPASS = os.environ.get("AGENT_REMOTE_INSECURE", "1") != "0"
TOKEN_ENV_VAR = "HERMES_AGENT_SERVER_TOKEN"


class AIAgentProxyError(RuntimeError):
    """Raised when a remote AIAgent call fails for any reason."""

    def __init__(self, message: str, *, kind: str = "remote"):
        super().__init__(message)
        self.kind = kind


class _RemoteContextCompressor:
    """Pass-through proxy for ``AIAgent.context_compressor.compress``."""

    def __init__(self, parent: "RemoteAIAgent"):
        self._parent = parent

    def compress(self, original_messages, current_tokens, focus_topic=None):
        return self._parent._call(
            method="compress",
            context=original_messages,
            approx_tokens=current_tokens,
            focus_topic=focus_topic,
        )


class RemoteAIAgent:
    """AIAgent drop-in replacement. Constructor stores kwargs; methods
    proxy via HTTPS to ``agent_remote_server``."""

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        base_url: str,
        api_key: str,
        platform: str = "webui",
        quiet_mode: bool = True,
        enabled_toolsets: Optional[Iterable[str]] = None,
        session_id: str,
    ):
        self._cfg = {
            "model": model,
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "platform": platform,
            "quiet_mode": quiet_mode,
            "enabled_toolsets": list(enabled_toolsets) if enabled_toolsets else [],
            "session_id": session_id,
        }
        self.context_compressor = _RemoteContextCompressor(self)

    # ------------------------------------------------------------------
    # Internal: one HTTP request -> one JSON-RPC reply
    # ------------------------------------------------------------------
    def _call(self, *, method: str, **kwargs) -> Any:
        url = os.environ.get("AGENT_REMOTE_URL", "https://127.0.0.1:9120")
        url = url.rstrip("/") + "/agent/run"
        token = os.environ.get(TOKEN_ENV_VAR, "")
        if not token:
            raise AIAgentProxyError(
                f"{TOKEN_ENV_VAR} env var is empty", kind="config")

        payload = {"method": method, "call_id": uuid.uuid4().hex,
                   **self._cfg, **kwargs}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "hermes-webui-agent-remote/1.0",
            },
        )

        ctx = None
        if CERT_BYPASS:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(
                req, context=ctx, timeout=DEFAULT_TIMEOUT,
            ) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise AIAgentProxyError(
                f"remote {exc.code} {exc.reason}: {err_body[:300]}",
                kind="http") from exc
        except urllib.error.URLError as exc:
            raise AIAgentProxyError(
                f"remote unreachable: {exc.reason}",
                kind="network") from exc
        except Exception as exc:
            raise AIAgentProxyError(
                f"remote call crashed: {exc}",
                kind="unknown") from exc

        try:
            envelope = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AIAgentProxyError(
                f"remote returned invalid JSON: {raw[:200]!r}",
                kind="protocol") from exc

        if not envelope.get("ok"):
            raise AIAgentProxyError(
                f"remote returned error: {envelope.get('error')!r}",
                kind="remote_error")

        return envelope.get("result")

    # ------------------------------------------------------------------
    # Public method surface mirroring AIAgent (only what webui uses)
    # ------------------------------------------------------------------
    def run_conversation(
        self,
        *,
        user_message: str,
        system_message: str = "",
        conversation_history: Optional[List[Any]] = None,
        task_id: str = "",
    ) -> Dict[str, Any]:
        return self._call(
            method="run_conversation",
            user_message=user_message,
            system_message=system_message,
            conversation_history=conversation_history or [],
            task_id=task_id,
        )

    def chat(
        self,
        message: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        # stream_callback is intentionally not forwarded because
        # agent_remote_server is sync only.
        result = self._call(method="chat", message=message)
        return result.get("final_response") if isinstance(result, dict) else ""


def ping_remote(url: Optional[str] = None, token: Optional[str] = None,
                timeout: float = 10) -> Dict[str, Any]:
    """Cheap health probe used by webui startup diagnostics."""
    target = (url or os.environ.get(
        "AGENT_REMOTE_URL", "https://127.0.0.1:9120")).rstrip("/")
    bearer = token if token is not None else os.environ.get(
        TOKEN_ENV_VAR, "")
    if not bearer:
        raise AIAgentProxyError(
            f"{TOKEN_ENV_VAR} env var is empty", kind="config")
    req = urllib.request.Request(
        f"{target}/agent/run",
        data=json.dumps({"method": "ping",
                          "call_id": uuid.uuid4().hex}).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {bearer}",
                 "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))
