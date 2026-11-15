"""The editor's HTTP server: the checks of every request, `Range`, static files. Synthetic media only."""

import pytest
from editor_fakes import HOST, RunningEditor, editor_settings, make_media

from vacation_memories.editor.server import EditorApp, parse_range, plan_error_field


PAGE = "<!doctype html><title>Vacation Memories</title><div id=root></div>"


@pytest.fixture(scope="module")
def editor(tmp_path_factory):
    media = make_media(tmp_path_factory.mktemp("server-media"))
    static = tmp_path_factory.mktemp("server-static")  # the built interface exists only in the image
    (static / "index.html").write_text(PAGE, encoding="utf-8")
    app = EditorApp(editor_settings(media, tmp_path_factory.mktemp("server-work")), static_dir=static)
    with RunningEditor(app) as running:
        yield running


class TestRange:
    @pytest.mark.parametrize(("header", "expected"), [
        (None, None),
        ("bytes=0-99", (0, 99)),
        ("bytes=10-", (10, 999)),
        ("bytes=-100", (900, 999)),
        ("bytes=990-5000", (990, 999)),
        ("bytes=1000-", "invalid"),
        ("bytes=500-100", "invalid"),
        ("bytes=-0", "invalid"),
        ("bytes=0-1,5-9", None),  # several ranges: served whole
        ("items=0-1", None),
        ("bytes=a-b", None),
    ])
    def test_a_single_range(self, header, expected):
        assert parse_range(header, 1000) == expected


def test_the_field_of_a_plan_error_is_its_path():
    assert plan_error_field("elements[3].effects[0]: unknown key(s) 'x'") == "elements[3].effects[0]"
    assert plan_error_field("elements[2]: 'duration' must be a number") == "elements[2].duration"
    assert plan_error_field("'output.width' must be an even integer") == "output.width"
    assert plan_error_field("the plan: unknown key(s) 'x'") is None


class TestRequestsFromTheEditorsOwnPageOnly:
    def test_health_answers_whatever_the_host(self, editor):
        status, _, body = editor.client.get("/api/health", headers={"Host": "editor:8000"})
        assert status == 200 and body["status"] == "ok"

    @pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:9999", "evil.example:8080", ""])
    def test_a_foreign_host_is_refused(self, editor, host):
        status, _, body = editor.client.get("/api/folders", headers={"Host": host})
        assert status == 403 and body["error"]["code"] == "forbidden_host"

    def test_the_page_checks_the_host_too(self, editor):
        assert editor.client.get("/", headers={"Host": "evil.example"})[0] == 403

    def test_both_allowed_hosts_work(self, editor):
        for host in ("127.0.0.1:8080", "localhost:8080", "LOCALHOST:8080"):
            assert editor.client.get("/api/folders", headers={"Host": host})[0] == 200

    @pytest.mark.parametrize("origin", [None, "http://evil.example", "http://127.0.0.1:9999", "null"])
    def test_a_change_needs_the_origin_of_the_page(self, editor, origin):
        status, _, body = editor.client.request("POST", "/api/projects", {"folder": "trip"}, {"Origin": origin})
        assert status == 403 and body["error"]["code"] == "forbidden_origin"

    @pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"])
    def test_a_change_needs_json(self, editor, content_type):
        status, _, body = editor.client.request("POST", "/api/projects", b'{"folder": "trip"}',
                                                {"Content-Type": content_type})
        assert status == 415 and body["error"]["code"] == "unsupported_media_type"

    def test_json_with_a_charset_is_accepted(self, editor):
        status, _, _ = editor.client.request("POST", "/api/projects", {"folder": "trip"},
                                             {"Content-Type": "application/json; charset=utf-8"})
        assert status in (200, 201)

    def test_nothing_is_refused_by_the_page_that_the_page_sends(self, editor):
        status, headers, _ = editor.client.get("/api/folders")
        assert status == 200
        assert not any(key.startswith("access-control-") for key in headers)  # no CORS

    def test_options_is_not_served_so_no_preflight_can_succeed(self, editor):
        status, headers, _ = editor.client.request("OPTIONS", "/api/folders", None, {"Origin": "http://evil.example"})
        assert status == 405 and "access-control-allow-origin" not in headers

    def test_the_hosts_can_be_configured_for_a_tunnel(self, tmp_path_factory):
        media = make_media(tmp_path_factory.mktemp("tunnel-media"))
        app = EditorApp(editor_settings(media, tmp_path_factory.mktemp("tunnel-work")), allowed_hosts=["127.0.0.1:9000"])
        with RunningEditor(app) as running:
            assert running.client.get("/api/folders", headers={"Host": "127.0.0.1:9000"})[0] == 200
            assert running.client.get("/api/folders", headers={"Host": HOST})[0] == 403
            status, _, _ = running.client.request("POST", "/api/projects", {"folder": "trip"},
                                                  {"Host": "127.0.0.1:9000", "Origin": "http://127.0.0.1:9000"})
            assert status in (200, 201)


class TestErrors:
    def test_errors_are_json_with_a_code(self, editor):
        status, headers, body = editor.client.get("/api/nowhere")
        assert status == 404 and headers["content-type"].startswith("application/json")
        assert body["error"]["code"] == "not_found"

    def test_a_wrong_method_is_a_405(self, editor):
        assert editor.client.request("PUT", "/api/folders", {})[0] == 405

    def test_invalid_json_is_a_400(self, editor):
        status, _, body = editor.client.request("POST", "/api/projects", b"{not json")
        assert status == 400 and body["error"]["code"] == "invalid_json"

    def test_an_unknown_project_is_a_404(self, editor):
        assert editor.client.get("/api/projects/nope-12345678")[0] == 404

    def test_an_oversized_body_is_refused(self, editor):
        status, _, body = editor.client.request("POST", "/api/projects", b" " * (9 * 1024 * 1024))
        assert status == 413 and body["error"]["code"] == "body_too_large"

    def test_responses_carry_the_security_headers(self, editor):
        _, headers, _ = editor.client.get("/api/folders")
        assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in headers["content-security-policy"]


class TestStaticPage:
    def test_the_page_is_served(self, editor):
        status, headers, body = editor.client.get("/")
        assert status == 200 and headers["content-type"].startswith("text/html")
        assert b"Vacation Memories" in body

    def test_an_unknown_path_gets_the_page_so_the_interface_can_route(self, editor):
        assert editor.client.get("/projects/some-thing")[0] == 200

    @pytest.mark.parametrize("path", ["/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/..%2f..%2fetc/passwd"])
    def test_it_cannot_leave_the_static_folder(self, editor, path):
        status, _, body = editor.client.get(path)
        assert b"root:" not in (body if isinstance(body, bytes) else b"")

    def test_the_served_page_loads_nothing_from_outside(self, editor):
        _, _, body = editor.client.get("/")
        assert b"http://" not in body.replace(b'http-equiv', b"") and b"https://" not in body
