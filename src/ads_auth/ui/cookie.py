"""Setting and clearing the HttpOnly sign-in cookie from inside a Streamlit app.

Streamlit reads cookies (`st.context.cookies`, from the websocket handshake) but offers no way to
set one, and an HttpOnly cookie can only come from an HTTP response. So the package adds one small
route to the running server, `POST <baseUrlPath>/_ads_auth/cookie`, and asks the browser to call
it from a zero-height iframe right after sign-in (and after sign-out, to clear the cookie). The
route verifies the token's signature with the app's secret before echoing it back as the cookie.

The route is registered on first use through the server object Streamlit keeps for the current
session's websocket. Streamlit 1.37 to 1.57 serve through Tornado; 1.58 and newer through
Starlette. Both are handled; anything else is reported as unavailable, in which case sign-in works
as it always did and a reload shows the sign-in screen.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime

from ads_auth import tokens

ENDPOINT = "_ads_auth/cookie"
REQUEST_HEADER = "X-Ads-Auth"           # same-origin JavaScript only; a cross-site form cannot send it
COOKIE_NAME = tokens.COOKIE_NAME
_LOG = logging.getLogger("ads_auth.cookie")
_lock = threading.Lock()
_registered: dict[str, bool] = {}       # endpoint path -> True once registered (process-wide, one app per process)
_unavailable_reported = False


# --- the decision, shared by both server backends and tested without either ---------------------

@dataclass(frozen=True)
class Decision:
    status: int                 # HTTP status for the response
    value: str | None = None    # cookie value to set (None: nothing to set)
    max_age: int = 0            # seconds the cookie lives
    clear: bool = False         # expire the cookie instead


def decide(secret: str, header: str | None, body: bytes | str, now: datetime | None = None) -> Decision:
    """What the endpoint should do for one request."""
    if header != "1":
        return Decision(400)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return Decision(400)
    if not isinstance(data, dict):
        return Decision(400)
    if data.get("clear") is True:
        return Decision(204, clear=True)
    claims = tokens.verify(secret, data.get("token"))
    if claims is None:
        return Decision(400)
    left = tokens.remaining_seconds(claims, now)
    if left <= 0:
        return Decision(400)
    return Decision(204, value=data["token"], max_age=left)


def is_secure_request(scheme: str | None, forwarded_proto: str | None) -> bool:
    """Mark the cookie Secure when the browser reached the app over HTTPS (directly or via a proxy)."""
    return scheme == "https" or (forwarded_proto or "").split(",")[0].strip().lower() == "https"


def cookie_path(base_url_path: str) -> str:
    base = (base_url_path or "").strip("/")
    return f"/{base}" if base else "/"


def endpoint_path(base_url_path: str) -> str:
    base = (base_url_path or "").strip("/")
    return f"/{base}/{ENDPOINT}" if base else f"/{ENDPOINT}"


def setter_html(path: str, action: tuple) -> str:
    """The page fragment that makes the browser call the endpoint. `action` is ("set", token) or ("clear",)."""
    payload = {"clear": True} if action[0] == "clear" else {"token": action[1]}
    return (
        "<script>fetch(" + json.dumps(path) + ",{method:\"POST\",credentials:\"same-origin\",keepalive:true,"
        "headers:{\"Content-Type\":\"application/json\",\"" + REQUEST_HEADER + "\":\"1\"},"
        "body:" + json.dumps(json.dumps(payload)) + "}).catch(function(){});</script>"
    )


# --- Streamlit side ------------------------------------------------------------------------------

def request_cookies(st_module) -> dict[str, str]:
    """The cookies the browser sent when this session's websocket connected."""
    try:
        return dict(st_module.context.cookies)
    except Exception:  # noqa: BLE001  (no runtime, or a Streamlit without st.context)
        return {}


def apply_action(st_module, settings, action: tuple) -> bool:
    """Ask the browser to set or clear the cookie. False when the endpoint cannot be provided."""
    if not ensure_endpoint(settings):
        return False
    _render(st_module, setter_html(endpoint_path(_base_url_path()), action))
    return True


def _render(st_module, html: str) -> None:
    # One pixel tall: `st.iframe` refuses a height of 0, and the frame only has to exist for its
    # script to run. `st.iframe` arrived in Streamlit 1.5x and replaces the deprecated components API.
    iframe = getattr(st_module, "iframe", None)
    if iframe is not None:
        iframe(html, height=1)
        return
    import streamlit.components.v1 as components
    components.html(html, height=1)


def _base_url_path() -> str:
    from streamlit import config
    return config.get_option("server.baseUrlPath") or ""


def ensure_endpoint(settings) -> bool:
    """Register the cookie endpoint on the running server once per process. False when there is none."""
    global _unavailable_reported
    path = endpoint_path(_base_url_path())
    with _lock:
        known = _registered.get(path)
        if known is not None:
            return known
        found = _server_app()
        if found is None:
            # No live server (tests, bare mode) or one this version of the package does not know.
            # Not cached: a later run may be served differently. Reported once per process.
            if not _unavailable_reported:
                _unavailable_reported = True
                _LOG.warning("ads_auth: no server to register the sign-in cookie endpoint on; "
                             "a browser reload will show the sign-in screen.")
            return False
        backend, app = found
        if backend == "tornado":
            _register_tornado(app, path, settings.cookie_secret)
        else:
            _register_starlette(app, path, settings.cookie_secret)
        _registered[path] = True
        return True


def _server_app():
    """("tornado"|"starlette", app) for the live server behind this session, or None when there is none.

    Only a real Tornado `Application` or Starlette app counts: AppTest hands out mock clients, and
    bare mode has no server at all; both are "none", not an error.
    """
    try:
        from streamlit import runtime
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except ImportError:
        return None
    if not runtime.exists():
        return None
    ctx = get_script_run_ctx(suppress_warning=True)
    if ctx is None:
        return None
    client = runtime.get_instance().get_client(ctx.session_id)
    if client is None:
        return None
    application = getattr(client, "application", None)           # Tornado: the handler's Application
    if application is not None:
        try:
            import tornado.web
        except ImportError:
            pass
        else:
            if isinstance(application, tornado.web.Application):
                return "tornado", application
    websocket = getattr(client, "_websocket", None)               # Starlette: the session client wraps a WebSocket
    scope = getattr(websocket, "scope", None)
    if isinstance(scope, dict):
        try:
            from starlette.applications import Starlette
        except ImportError:
            pass
        else:
            app = scope.get("app")
            if isinstance(app, Starlette):
                return "starlette", app
    return None


def _cookie_kwargs(secure: bool) -> dict:
    return {"path": cookie_path(_base_url_path()), "httponly": True, "samesite": "Lax", "secure": secure}


def _register_tornado(application, path: str, secret: str) -> None:
    import tornado.web

    class CookieHandler(tornado.web.RequestHandler):
        def initialize(self, secret: str) -> None:
            self._secret = secret

        def check_xsrf_cookie(self) -> None:
            pass                                    # replaced by the custom-header check in `decide`

        def set_default_headers(self) -> None:
            self.set_header("Cache-Control", "no-store")

        def post(self) -> None:
            decision = decide(self._secret, self.request.headers.get(REQUEST_HEADER), self.request.body)
            secure = is_secure_request(self.request.protocol, self.request.headers.get("X-Forwarded-Proto"))
            if decision.clear:
                self.clear_cookie(COOKIE_NAME, **_cookie_kwargs(secure))
            elif decision.value:
                self.set_cookie(COOKIE_NAME, decision.value, max_age=decision.max_age, **_cookie_kwargs(secure))
            self.set_status(decision.status)

    application.add_handlers(r".*$", [(f"^{path}/?$", CookieHandler, {"secret": secret})])


def _register_starlette(app, path: str, secret: str) -> None:
    from starlette.responses import Response
    from starlette.routing import Route

    async def endpoint(request):
        body = await request.body()
        decision = decide(secret, request.headers.get(REQUEST_HEADER), body)
        secure = is_secure_request(request.url.scheme, request.headers.get("X-Forwarded-Proto"))
        response = Response(status_code=decision.status, headers={"Cache-Control": "no-store"})
        kwargs = _cookie_kwargs(secure)
        kwargs["samesite"] = kwargs["samesite"].lower()
        if decision.clear:
            response.delete_cookie(COOKIE_NAME, **kwargs)
        elif decision.value:
            response.set_cookie(COOKIE_NAME, decision.value, max_age=decision.max_age, **kwargs)
        return response

    app.router.routes.insert(0, Route(path, endpoint, methods=["POST"]))
