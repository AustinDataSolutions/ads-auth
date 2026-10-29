"""The cookie endpoint: the decision it makes, the page fragment that calls it, and both server backends."""
import asyncio
import json
from datetime import timedelta
from http.cookies import SimpleCookie

import pytest

from ads_auth import tokens
from ads_auth.db import to_iso, utc_now
from ads_auth.ui import cookie

SECRET = "c" * 32


def _token(hours_ago=0):
    return tokens.issue(SECRET, 5, 2, to_iso(utc_now() - timedelta(hours=hours_ago)))


# --- the decision --------------------------------------------------------------------------------

def test_a_valid_token_is_set_for_the_rest_of_the_session():
    token = _token(hours_ago=2)
    d = cookie.decide(SECRET, "1", json.dumps({"token": token}))
    assert d.status == 204 and d.value == token and not d.clear
    assert abs(d.max_age - 6 * 3600) <= 2


def test_clear_expires_the_cookie():
    d = cookie.decide(SECRET, "1", b'{"clear": true}')
    assert d.status == 204 and d.clear and d.value is None


@pytest.mark.parametrize("header, body", [
    (None, json.dumps({"token": "x"})),                    # no custom header: not our page's JavaScript
    ("", json.dumps({"token": "x"})),
    ("1", "not json"),
    ("1", "[]"),
    ("1", json.dumps({"token": "v1.5.2.1.sig"})),           # bad signature
    ("1", json.dumps({"token": None})),
    ("1", json.dumps({"clear": "yes"})),
])
def test_anything_else_is_a_bad_request_with_no_cookie(header, body):
    d = cookie.decide(SECRET, header, body)
    assert d.status == 400 and d.value is None and not d.clear


def test_an_expired_token_is_not_set_again():
    d = cookie.decide(SECRET, "1", json.dumps({"token": _token(hours_ago=9)}))
    assert d.status == 400 and d.value is None


def test_the_secret_decides_which_tokens_count():
    other = tokens.issue("d" * 32, 5, 2, to_iso(utc_now()))
    assert cookie.decide(SECRET, "1", json.dumps({"token": other})).status == 400


def test_secure_flag_follows_https_directly_or_through_a_proxy():
    assert cookie.is_secure_request("https", None)
    assert cookie.is_secure_request("http", "https")
    assert cookie.is_secure_request("http", "https, http")
    assert not cookie.is_secure_request("http", None)
    assert not cookie.is_secure_request("http", "http")


def test_paths_follow_the_base_url_path():
    assert cookie.endpoint_path("") == "/_ads_auth/cookie" and cookie.cookie_path("") == "/"
    assert cookie.endpoint_path("/tools/") == "/tools/_ads_auth/cookie" and cookie.cookie_path("tools") == "/tools"


def test_the_page_fragment_posts_the_token_with_the_header_and_keeps_the_request_alive():
    html = cookie.setter_html("/_ads_auth/cookie", ("set", "v1.5.2.1.sig"))
    assert html.startswith("<script>") and 'fetch("/_ads_auth/cookie"' in html
    assert 'method:"POST"' in html and "keepalive:true" in html and 'credentials:"same-origin"' in html
    assert '"X-Ads-Auth":"1"' in html and '\\"token\\": \\"v1.5.2.1.sig\\"' in html
    assert '\\"clear\\": true' in cookie.setter_html("/_ads_auth/cookie", ("clear",))


def test_the_fragment_is_drawn_by_the_installed_streamlit_without_error(tmp_path):
    """`_render` picks `st.iframe` or the components API; whichever it is must accept the fragment."""
    from streamlit.testing.v1 import AppTest
    script = tmp_path / "draw.py"
    script.write_text(
        "import streamlit as st\n"
        "from ads_auth.ui import cookie\n"
        "cookie._render(st, cookie.setter_html('/_ads_auth/cookie', ('clear',)))\n"
        "st.write('drawn')\n"
    )
    at = AppTest.from_file(str(script), default_timeout=30).run()
    assert not at.exception, at.exception
    assert [m.value for m in at.markdown] == ["drawn"]


# --- the server backends ---------------------------------------------------------------------------

def _parse_set_cookie(header: str) -> SimpleCookie:
    jar = SimpleCookie()
    jar.load(header)
    return jar


tornado_testing = pytest.importorskip("tornado.testing", reason="Streamlit 1.58+ serves through Starlette")


class TornadoBackend(tornado_testing.AsyncHTTPTestCase):
    """The endpoint on a Tornado application, as Streamlit 1.57 and older build it."""

    def get_app(self):
        import tornado.web
        app = tornado.web.Application([], cookie_secret="x" * 32, xsrf_cookies=True)
        cookie._register_tornado(app, "/_ads_auth/cookie", SECRET)
        return app

    def _post(self, body, headers=None, path="/_ads_auth/cookie"):
        return self.fetch(path, method="POST", body=body, headers=headers or {}, raise_error=False)

    def test_sets_an_httponly_lax_cookie_for_a_valid_token(self):
        token = _token(hours_ago=1)
        r = self._post(json.dumps({"token": token}), {"X-Ads-Auth": "1", "X-Forwarded-Proto": "https"})
        assert r.code == 204
        jar = _parse_set_cookie(r.headers["Set-Cookie"])
        morsel = jar[tokens.COOKIE_NAME]
        assert morsel.value == token
        assert morsel["httponly"] and morsel["samesite"] == "Lax" and morsel["secure"] and morsel["path"] == "/"
        assert abs(int(morsel["max-age"]) - 7 * 3600) <= 2
        assert r.headers["Cache-Control"] == "no-store"

    def test_plain_http_sets_no_secure_flag(self):
        r = self._post(json.dumps({"token": _token()}), {"X-Ads-Auth": "1"})
        assert r.code == 204 and not _parse_set_cookie(r.headers["Set-Cookie"])[tokens.COOKIE_NAME]["secure"]

    def test_clear_expires_the_cookie(self):
        r = self._post('{"clear": true}', {"X-Ads-Auth": "1"})
        assert r.code == 204
        morsel = _parse_set_cookie(r.headers["Set-Cookie"])[tokens.COOKIE_NAME]
        assert morsel.value == "" and morsel["expires"] and morsel["httponly"]

    def test_without_the_header_tornados_xsrf_check_is_replaced_by_a_400_and_no_cookie(self):
        r = self._post(json.dumps({"token": _token()}))
        assert r.code == 400 and "Set-Cookie" not in r.headers

    def test_get_is_not_served(self):
        assert self.fetch("/_ads_auth/cookie", raise_error=False).code == 405


# --- the Starlette backend, driven as a bare ASGI app (no HTTP client needed) ---------------------

starlette = pytest.importorskip("starlette", reason="Streamlit 1.57 and older serve through Tornado")


def _asgi_post(app, body: bytes, headers: dict, scheme="http", path="/_ads_auth/cookie", method="POST"):
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": scheme,
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8501),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers_out = [(k.decode(), v.decode()) for k, v in start["headers"]]
    return start["status"], headers_out


@pytest.fixture
def starlette_app():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def catch_all(request):
        return PlainTextResponse("index", status_code=200)

    app = Starlette(routes=[Route("/{path:path}", catch_all, methods=["GET", "POST"])])
    cookie._register_starlette(app, "/_ads_auth/cookie", SECRET)
    return app


def test_starlette_sets_an_httponly_lax_cookie_for_a_valid_token(starlette_app):
    token = _token(hours_ago=1)
    status, headers = _asgi_post(starlette_app, json.dumps({"token": token}).encode(),
                                 {"X-Ads-Auth": "1", "X-Forwarded-Proto": "https", "content-type": "application/json"})
    assert status == 204
    set_cookie = dict(headers)["set-cookie"]
    morsel = _parse_set_cookie(set_cookie)[tokens.COOKIE_NAME]
    assert morsel.value == token and morsel["httponly"] and morsel["secure"] and morsel["path"] == "/"
    assert morsel["samesite"].lower() == "lax" and abs(int(morsel["max-age"]) - 7 * 3600) <= 2
    assert dict(headers)["cache-control"] == "no-store"


def test_starlette_clear_and_bad_requests(starlette_app):
    status, headers = _asgi_post(starlette_app, b'{"clear": true}', {"X-Ads-Auth": "1"})
    morsel = _parse_set_cookie(dict(headers)["set-cookie"])[tokens.COOKIE_NAME]
    assert status == 204 and morsel.value == "" and (morsel["max-age"] == "0" or morsel["expires"])
    status, headers = _asgi_post(starlette_app, json.dumps({"token": _token()}).encode(), {})
    assert status == 400 and "set-cookie" not in dict(headers)


def test_starlette_get_never_reaches_the_endpoint(starlette_app):
    # Starlette prefers a route matching path and method over one matching the path alone, so a GET
    # falls through to the app's own routes (Streamlit's page fallback), and 405 only without one.
    status, headers = _asgi_post(starlette_app, b"", {}, method="GET")
    assert status == 200 and "set-cookie" not in dict(headers) and "cache-control" not in dict(headers)
    from starlette.applications import Starlette
    bare = Starlette()
    cookie._register_starlette(bare, "/_ads_auth/cookie", SECRET)
    status, headers = _asgi_post(bare, b"", {}, method="GET")
    assert status == 405 and dict(headers)["allow"] == "POST"


def test_starlette_route_is_tried_before_the_catch_all(starlette_app):
    status, _ = _asgi_post(starlette_app, b"{}", {"X-Ads-Auth": "1"})
    assert status == 400                                    # ours answered, not the catch-all's 200
