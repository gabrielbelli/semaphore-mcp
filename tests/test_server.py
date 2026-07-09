"""Offline tests for the generated Semaphore MCP surface.

These guard the safety model, not the live API:
  * generation is reproducible from the pinned spec + allowlist
  * only curated tools are exposed (no DELETE/PUT ever)
  * read tools carry readOnlyHint=True; write tools do not
No network access — tools return a graceful error when env is unset.
"""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {
    "list_projects", "get_project", "list_templates", "get_template",
    "list_tasks", "get_last_tasks", "get_task", "get_task_output",
    "list_inventory", "get_inventory", "list_environment",
    "run_task", "stop_task",
}
WRITE_TOOLS = {"run_task", "stop_task"}


@pytest.fixture(scope="module")
def mcp():
    # regenerate then import the fresh server module
    subprocess.run([sys.executable, "generate.py"], cwd=ROOT, check=True)
    sys.path.insert(0, str(ROOT))
    import server  # noqa: E402
    return server.mcp


@pytest.fixture(scope="module")
def tools(mcp):
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_exact_curated_surface(tools):
    assert set(tools) == EXPECTED


def test_no_destructive_verbs_leak(tools):
    # descriptions carry the underlying "METHOD /path"; assert none mutate-delete
    for name, t in tools.items():
        assert not t.description.startswith("DELETE ")
        assert not t.description.startswith("PUT ")


def test_read_tools_are_readonly(tools):
    for name, t in tools.items():
        ro = t.annotations.readOnlyHint if t.annotations else None
        if name in WRITE_TOOLS:
            assert ro is False, f"{name} must not be readOnly"
        else:
            assert ro is True, f"{name} must be readOnly"


def test_generation_is_reproducible(mcp, tools):
    # a second generate produces the identical surface
    subprocess.run([sys.executable, "generate.py"], cwd=ROOT, check=True)
    assert set(tools) == EXPECTED
