"""Offline tests for the generated Semaphore MCP surface.

These guard the safety model, not the live API:
  * generation is reproducible from the pinned spec + allowlist
  * only curated tools are exposed (three tiers: read / write / delete)
  * read tools carry readOnlyHint=True; writes do not
  * delete tools carry destructiveHint=True AND refuse without confirm=True
No network access — tools return a graceful error when env is unset.
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
        assert tools[name].annotations.readOnlyHint is True, f"{name} must be readOnly"


def test_write_tools_are_not_readonly_nor_destructive(tools):
    for name in WRITE_TOOLS:
        a = tools[name].annotations
        assert a.readOnlyHint is False, f"{name} must not be readOnly"
        assert a.destructiveHint is False, f"{name} must not be destructive"


def test_delete_tools_are_flagged_destructive(tools):
    for name in DELETE_TOOLS:
        a = tools[name].annotations
        assert a.readOnlyHint is False, f"{name} must not be readOnly"
        assert a.destructiveHint is True, f"{name} must be destructive"


def test_delete_refuses_without_confirmation(server_mod):
    # the in-server guard: a delete with confirm defaulting False must NOT
    # reach the network — it returns the refusal payload instead.
    out = asyncio.run(server_mod.delete_task(project_id=1, task_id=1))
    assert "destructive operation refused" in out
    # and confirm=True gets past the guard (fails later on unset env, not the gate)
    out2 = asyncio.run(server_mod.delete_task(project_id=1, task_id=1, confirm=True))
    assert "destructive operation refused" not in out2


def test_update_tools_require_only_ids(tools):
    # read-modify-write: a rename must be possible without re-supplying every
    # field, so the ONLY required params are the path ids (project_id + resource).
    for name in UPDATE_TOOLS:
        required = set(tools[name].inputSchema.get("required", []))
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
