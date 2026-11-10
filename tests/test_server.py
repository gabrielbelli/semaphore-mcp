"""Offline tests for the generated Semaphore MCP surface.

These guard the safety model, not the live API:
  * generation is reproducible from the pinned spec + allowlist
  * only curated tools are exposed (three tiers: read / write / delete)
  * read tools carry readOnlyHint=True; writes do not
  * delete tools carry destructiveHint=True AND refuse without confirm=True
No network access: the tests clear SEMAPHORE_* from the environment, so a tool
returns a graceful error and never calls the Semaphore the shell points at.
"""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

READ_TOOLS = {
    "list_projects", "get_project", "list_templates", "get_template",
    "list_tasks", "get_last_tasks", "get_task", "get_task_output",
    "list_inventory", "get_inventory", "list_environment", "list_repositories",
}
WRITE_TOOLS = {
    "run_task", "stop_task",
    "create_template", "update_template",
    "create_environment", "update_environment",
    "create_inventory", "update_inventory",
    "create_repository", "update_repository",
}
UPDATE_TOOLS = {
    "update_template", "update_environment", "update_inventory", "update_repository",
}
DELETE_TOOLS = {
    "delete_template", "delete_environment", "delete_inventory",
    "delete_repository", "delete_task",
}
EXPECTED = READ_TOOLS | WRITE_TOOLS | DELETE_TOOLS


@pytest.fixture(scope="session", autouse=True)
def _no_real_semaphore():
    # server.py reads SEMAPHORE_URL and SEMAPHORE_TOKEN at import. With them
    # exported in the shell, a delete_task(confirm=True) below sent a real
    # DELETE to that Semaphore. Clear them before anything imports the server.
    with pytest.MonkeyPatch.context() as mp:
        for var in ("SEMAPHORE_URL", "SEMAPHORE_TOKEN", "SEMAPHORE_USER_AGENT"):
            mp.delenv(var, raising=False)
        yield


@pytest.fixture(scope="module")
def server_mod():
    # regenerate then import the fresh server module
    subprocess.run([sys.executable, "generate.py"], cwd=ROOT, check=True)
    sys.path.insert(0, str(ROOT))
    import server  # noqa: E402
    return server


@pytest.fixture(scope="module")
def mcp(server_mod):
    return server_mod.mcp


@pytest.fixture(scope="module")
def tools(mcp):
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_exact_curated_surface(tools):
    assert set(tools) == EXPECTED


def test_read_tools_are_readonly(tools):
    for name in READ_TOOLS:
        assert tools[name].annotations.read_only_hint is True, f"{name} must be readOnly"


def test_write_tools_are_not_readonly_nor_destructive(tools):
    for name in WRITE_TOOLS:
        a = tools[name].annotations
        assert a.read_only_hint is False, f"{name} must not be readOnly"
        assert a.destructive_hint is False, f"{name} must not be destructive"


def test_delete_tools_are_flagged_destructive(tools):
    for name in DELETE_TOOLS:
        a = tools[name].annotations
        assert a.read_only_hint is False, f"{name} must not be readOnly"
        assert a.destructive_hint is True, f"{name} must be destructive"


def test_delete_refuses_without_confirmation(server_mod, monkeypatch):
    # the in-server guard: a delete with confirm defaulting False must NOT
    # reach the network — it returns the refusal payload instead.
    monkeypatch.setattr(server_mod, "BASE", "")
    monkeypatch.setattr(server_mod, "TOKEN", "")
    out = asyncio.run(server_mod.delete_task(project_id=1, task_id=1))
    assert "destructive operation refused" in out
    # and confirm=True gets past the guard, then stops on the unset env, not
    # on the gate and not at a real Semaphore
    out2 = asyncio.run(server_mod.delete_task(project_id=1, task_id=1, confirm=True))
    assert "destructive operation refused" not in out2
    assert "SEMAPHORE_URL / SEMAPHORE_TOKEN not set" in out2


def test_update_tools_require_only_ids(tools):
    # read-modify-write: a rename must be possible without re-supplying every
    # field, so the ONLY required params are the path ids (project_id + resource).
    for name in UPDATE_TOOLS:
        required = set(tools[name].input_schema.get("required", []))
        assert required <= {"project_id", "template_id", "environment_id",
                            "inventory_id", "repository_id"}, \
            f"{name} should not force non-id fields (would blank on rename): {required}"


class _FakeResp:
    def __init__(self, status_code, content, ct="application/json"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": ct}

    def raise_for_status(self):
        pass

    def json(self):
        import json
        return json.loads(self.content)  # blows up on empty body, like httpx


def _fake_client(resp):
    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def request(self, *a, **k):
            return resp
    return _C


def test_204_empty_body_is_success_not_error(server_mod, monkeypatch):
    # Semaphore returns 204 No Content on DELETE/PUT; must not surface as a
    # JSON-parse "error", it's a success.
    monkeypatch.setattr(server_mod, "BASE", "https://x")
    monkeypatch.setattr(server_mod, "TOKEN", "t")
    monkeypatch.setattr(server_mod.httpx, "AsyncClient", _fake_client(_FakeResp(204, b"")))
    out = asyncio.run(server_mod.delete_task(1, 1, confirm=True))
    assert '"ok": true' in out
    assert "error" not in out


def test_upstream_http_error_reaches_the_client(server_mod, monkeypatch):
    # mcp 2 replaces an unexpected exception's text with a bare "Error executing
    # tool <name>". A WAF's redirect or an RBAC 403 must still tell the client
    # what went wrong, so call the tool as a client does, not the bare function.
    from mcp import Client

    httpx = server_mod.httpx
    real = httpx.AsyncClient

    def _redirect(request):
        return httpx.Response(302, headers={"location": "https://x/challenge"}, request=request)

    monkeypatch.setattr(server_mod, "BASE", "https://x")
    # long enough that redacting it cannot eat letters of the error text
    monkeypatch.setattr(server_mod, "TOKEN", "dummy-token")
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **k: real(transport=httpx.MockTransport(_redirect), **k))

    async def _call():
        async with Client(server_mod.mcp) as c:
            return await c.call_tool("list_projects", {})

    res = asyncio.run(_call())
    assert res.is_error is True
    text = res.content[0].text
    assert text.startswith("Error executing tool list_projects: ")
    assert "Redirect response '302 Found'" in text


def test_update_preserves_unspecified_fields(server_mod, monkeypatch):
    # rename-only update must carry the fetched object's other fields through.
    captured = {}

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def request(self, method, url, headers=None, params=None, json=None):
            import json as _j
            if method == "GET":
                return _FakeResp(200, _j.dumps({
                    "id": 7, "name": "old", "playbook": "deploy.yml",
                    "arguments": "[\"--check\"]", "inventory_id": 3,
                }).encode())
            captured["method"] = method
            captured["body"] = json
            return _FakeResp(204, b"")

    monkeypatch.setattr(server_mod, "BASE", "https://x")
    monkeypatch.setattr(server_mod, "TOKEN", "t")
    monkeypatch.setattr(server_mod.httpx, "AsyncClient", _C)
    asyncio.run(server_mod.update_template(project_id=1, template_id=7, name="new"))
    assert captured["method"] == "PUT"
    b = captured["body"]
    assert b["name"] == "new"                 # the change applied
    assert b["playbook"] == "deploy.yml"      # untouched field preserved
    assert b["arguments"] == '["--check"]'    # would have been blanked before
    assert b["id"] == 7 and b["project_id"] == 1


def test_generation_is_reproducible(tools):
    # a second generate produces the identical surface
    subprocess.run([sys.executable, "generate.py"], cwd=ROOT, check=True)
    assert set(tools) == EXPECTED


def test_requests_carry_the_servers_own_user_agent(server_mod, monkeypatch):
    # A WAF in front of Semaphore redirected python-httpx's default User-Agent
    # to a challenge page, and every tool failed. Each request names the server
    # and its version, and nothing such a rule matches on.
    captured = {}

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def request(self, method, url, headers=None, params=None, json=None):
            captured["headers"] = headers
            return _FakeResp(200, b"[]")

    monkeypatch.setattr(server_mod, "BASE", "https://x")
    monkeypatch.setattr(server_mod, "TOKEN", "t")
    monkeypatch.setattr(server_mod.httpx, "AsyncClient", _C)
    asyncio.run(server_mod.list_projects())
    ua = captured["headers"]["User-Agent"]
    assert ua.startswith(f"semaphore-mcp/{server_mod.__version__} ")
    assert "python" not in ua.lower() and "httpx" not in ua.lower()


def _fresh_server(monkeypatch, **env):
    # server.py reads its env at import, so load a fresh copy with env set.
    import importlib.util
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("server_fresh", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_user_agent_can_be_replaced(server_mod, monkeypatch):
    mod = _fresh_server(monkeypatch, SEMAPHORE_USER_AGENT="custom/1")
    assert mod.USER_AGENT == "custom/1"


@pytest.mark.parametrize("token, stripped", [
    ("dummy-token-value\r", True),    # CRLF env file
    ("dummy-token-value\n", True),    # secret file with a final newline
    ("dummy-token\nvalue", False),    # a newline that strip() cannot remove
])
def test_token_never_reaches_the_client(server_mod, monkeypatch, token, stripped):
    # h11 refuses a header with a CR or LF in it, and its error quotes the
    # header: "Illegal header value b'Bearer <token>'". As a ToolError, that
    # text went to the model, token and all.
    import h11
    from mcp import Client

    httpx = server_mod.httpx
    real = httpx.AsyncClient

    def _h11_checks_headers(request):
        # the check httpx's real transport runs before it sends, without a socket
        try:
            h11.Request(method=request.method, target=request.url.raw_path,
                        headers=list(request.headers.raw))
        except h11.LocalProtocolError as exc:
            raise httpx.LocalProtocolError(str(exc), request=request) from exc
        return httpx.Response(200, json=[], request=request)

    mod = _fresh_server(monkeypatch, SEMAPHORE_URL="https://x", SEMAPHORE_TOKEN=token)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **k: real(transport=httpx.MockTransport(_h11_checks_headers), **k))

    async def _call():
        async with Client(mod.mcp) as c:
            return await c.call_tool("list_projects", {})

    res = asyncio.run(_call())
    text = res.content[0].text
    assert "dummy-token" not in text
    if stripped:
        # a trailing CR or LF is gone at load, so the call goes through
        assert res.is_error is False and text == "[]"
    else:
        # still refused, and the error still says why
        assert res.is_error is True
        assert text == ("Error executing tool list_projects: "
                        "Illegal header value b'Bearer ***'")
