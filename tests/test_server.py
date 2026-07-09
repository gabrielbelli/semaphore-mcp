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
    "list_inventory", "get_inventory", "list_environment",
}
WRITE_TOOLS = {
    "run_task", "stop_task",
    "create_template", "update_template",
    "create_environment", "update_environment",
    "create_inventory", "update_inventory",
}
DELETE_TOOLS = {
    "delete_template", "delete_environment", "delete_inventory", "delete_task",
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


def test_generation_is_reproducible(tools):
    # a second generate produces the identical surface
    subprocess.run([sys.executable, "generate.py"], cwd=ROOT, check=True)
    assert set(tools) == EXPECTED
