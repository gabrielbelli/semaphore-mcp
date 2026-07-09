#!/usr/bin/env python3
"""Generate a curated FastMCP server from Semaphore's OpenAPI (Swagger 2.0) spec.

Why a generator and not a generic bridge:
  * Semaphore's spec ships EMPTY operationIds -> a naive bridge emits unusable
    tool names. We synthesise clean names here.
  * The raw spec is 127 operations. We expose a curated ~23 across three tiers.
  * Annotations (readOnlyHint / destructiveHint) are derived from the tool's
    declared `kind`, so hints are honest and automatic.

Safety model (three tiers, NOT read-only):
  * "read"   -> readOnlyHint=True                       (silent-allow candidate)
  * "write"  -> state-changing, NOT destructive         (ask candidate)
                create/update templates, environments (vars), inventories; run.
  * "delete" -> destructiveHint=True + in-server guard  (double-gated)
                the tool REFUSES unless called with confirm=True, so a stray
                delete can never fire on the first shot.

Run:  python generate.py  ->  writes server.py
"""
import re
import yaml

SPEC = "api-docs.yml"
OUT = "server.py"

REQ = None  # sentinel: a body field with default REQ has no default in the signature

# --- Curated allowlist --------------------------------------------------------
# Each entry: dict(name, method, path, kind[, body]).
#   body: list of (arg_name, json_key, annotation, default_src)
#         default_src is a literal string emitted after "=", or REQ for required.
# project_id (and any trailing {*_id}) is injected into write bodies automatically.
ALLOWLIST = [
    # --- reads ---------------------------------------------------------------
    dict(name="list_projects",    method="GET", path="/projects",                                      kind="read"),
    dict(name="get_project",      method="GET", path="/project/{project_id}/",                         kind="read"),
    dict(name="list_templates",   method="GET", path="/project/{project_id}/templates",                kind="read"),
    dict(name="get_template",     method="GET", path="/project/{project_id}/templates/{template_id}",  kind="read"),
    dict(name="list_tasks",       method="GET", path="/project/{project_id}/tasks",                    kind="read"),
    dict(name="get_last_tasks",   method="GET", path="/project/{project_id}/tasks/last",               kind="read"),
    dict(name="get_task",         method="GET", path="/project/{project_id}/tasks/{task_id}",          kind="read"),
    dict(name="get_task_output",  method="GET", path="/project/{project_id}/tasks/{task_id}/output",   kind="read"),
    dict(name="list_inventory",   method="GET", path="/project/{project_id}/inventory",                kind="read"),
    dict(name="get_inventory",    method="GET", path="/project/{project_id}/inventory/{inventory_id}", kind="read"),
    dict(name="list_environment", method="GET", path="/project/{project_id}/environment",              kind="read"),
    dict(name="list_repositories",method="GET", path="/project/{project_id}/repositories",             kind="read"),

    # --- writes (state-changing, non-destructive) ----------------------------
    dict(name="run_task",  method="POST", path="/project/{project_id}/tasks", kind="write", body=[
        ("template_id", "template_id", "int", REQ),
        ("environment", "environment", "str", '""'),
        ("limit",       "limit",       "str", '""'),
    ]),
    dict(name="stop_task", method="POST", path="/project/{project_id}/tasks/{task_id}/stop", kind="write"),

    dict(name="create_template", method="POST", path="/project/{project_id}/templates", kind="write", body=[
        ("name",          "name",          "str", REQ),
        ("playbook",      "playbook",      "str", REQ),
        ("inventory_id",  "inventory_id",  "int", REQ),
        ("repository_id", "repository_id", "int", REQ),
        ("environment_id","environment_id","int", REQ),
        ("app",           "app",           "str", '"ansible"'),
        ("git_branch",    "git_branch",    "Optional[str]", "None"),
        ("description",   "description",   "Optional[str]", "None"),
    ]),
    # update_* are read-modify-write (update=True): fields default None and only
    # the ones you pass overlay the fetched object, so omitting a field preserves
    # it instead of blanking it.
    dict(name="update_template", method="PUT", path="/project/{project_id}/templates/{template_id}", kind="write", update=True, body=[
        ("name",                       "name",                       "Optional[str]", "None"),
        ("playbook",                   "playbook",                   "Optional[str]", "None"),
        ("inventory_id",               "inventory_id",               "Optional[int]", "None"),
        ("repository_id",              "repository_id",              "Optional[int]", "None"),
        ("environment_id",             "environment_id",             "Optional[int]", "None"),
        ("app",                        "app",                        "Optional[str]", "None"),
        ("git_branch",                 "git_branch",                 "Optional[str]", "None"),
        ("description",                "description",                "Optional[str]", "None"),
        ("arguments",                  "arguments",                  "Optional[str]", "None"),   # JSON array string
        ("allow_override_args_in_task","allow_override_args_in_task","Optional[bool]", "None"),
    ]),

    dict(name="create_environment", method="POST", path="/project/{project_id}/environment", kind="write", body=[
        ("name",     "name",     "str", REQ),
        ("json_vars","json",     "str", '"{}"'),  # extra CLI vars, JSON string
        ("env_vars", "env",      "str", '"{}"'),  # environment vars, JSON string
        ("password", "password", "Optional[str]", "None"),
    ]),
    # strip `secrets`: the API returns secret metadata without values, so echoing
    # it back on PUT is meaningless; omitting it leaves stored secrets intact.
    dict(name="update_environment", method="PUT", path="/project/{project_id}/environment/{environment_id}", kind="write", update=True, strip=["secrets"], body=[
        ("name",     "name",     "Optional[str]", "None"),
        ("json_vars","json",     "Optional[str]", "None"),
        ("env_vars", "env",      "Optional[str]", "None"),
        ("password", "password", "Optional[str]", "None"),
    ]),

    dict(name="create_inventory", method="POST", path="/project/{project_id}/inventory", kind="write", body=[
        ("name",         "name",         "str", REQ),
        ("inventory",    "inventory",    "str", REQ),
        ("type",         "type",         "str", '"static"'),
        ("ssh_key_id",   "ssh_key_id",   "Optional[int]", "None"),
        ("become_key_id","become_key_id","Optional[int]", "None"),
        ("repository_id","repository_id","Optional[int]", "None"),
    ]),
    dict(name="update_inventory", method="PUT", path="/project/{project_id}/inventory/{inventory_id}", kind="write", update=True, body=[
        ("name",         "name",         "Optional[str]", "None"),
        ("inventory",    "inventory",    "Optional[str]", "None"),
        ("type",         "type",         "Optional[str]", "None"),
        ("ssh_key_id",   "ssh_key_id",   "Optional[int]", "None"),
        ("become_key_id","become_key_id","Optional[int]", "None"),
        ("repository_id","repository_id","Optional[int]", "None"),
    ]),

    # repositories: branch is pinned at the repo level, so branch testing needs a
    # dedicated repo object -> full CRUD.
    dict(name="create_repository", method="POST", path="/project/{project_id}/repositories", kind="write", body=[
        ("name",       "name",       "str", REQ),
        ("git_url",    "git_url",    "str", REQ),
        ("git_branch", "git_branch", "str", '"main"'),
        ("ssh_key_id", "ssh_key_id", "int", REQ),
    ]),
    dict(name="update_repository", method="PUT", path="/project/{project_id}/repositories/{repository_id}", kind="write", update=True, body=[
        ("name",       "name",       "Optional[str]", "None"),
        ("git_url",    "git_url",    "Optional[str]", "None"),
        ("git_branch", "git_branch", "Optional[str]", "None"),
        ("ssh_key_id", "ssh_key_id", "Optional[int]", "None"),
    ]),

    # --- deletes (destructive, in-server confirm gate) -----------------------
    dict(name="delete_template",    method="DELETE", path="/project/{project_id}/templates/{template_id}",   kind="delete"),
    dict(name="delete_environment", method="DELETE", path="/project/{project_id}/environment/{environment_id}", kind="delete"),
    dict(name="delete_inventory",   method="DELETE", path="/project/{project_id}/inventory/{inventory_id}", kind="delete"),
    dict(name="delete_repository",  method="DELETE", path="/project/{project_id}/repositories/{repository_id}", kind="delete"),
    dict(name="delete_task",        method="DELETE", path="/project/{project_id}/tasks/{task_id}",           kind="delete"),
]

HEADER = '''#!/usr/bin/env python3
"""Semaphore MCP server (POC) - GENERATED by generate.py. Do not edit by hand.

Three tiers, guardrailed — NOT read-only:
  read   -> safe to auto-allow
  write  -> create/update/run; state-changing but non-destructive
  delete -> destructive; refuses unless called with confirm=True

update_* tools read-modify-write: they GET the current object, overlay only the
fields you pass, then PUT the merged result — so a rename can't blank the fields
you left out. (Environment secrets are write-only in the API, so they are never
echoed back; omitting them leaves stored secrets untouched.)

Env:
  SEMAPHORE_URL    e.g. https://semaphore.example.com
  SEMAPHORE_TOKEN  bearer API token (scope RBAC to match the tier you allow)
"""
import os
import sys
import json
import asyncio
from typing import Optional
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

BASE = os.environ.get("SEMAPHORE_URL", "").rstrip("/")
TOKEN = os.environ.get("SEMAPHORE_TOKEN", "")

mcp = FastMCP("semaphore")


async def _request(method, path, query=None, body=None):
    if not BASE or not TOKEN:
        return {"error": "SEMAPHORE_URL / SEMAPHORE_TOKEN not set"}
    url = BASE + "/api" + path
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, url, headers=headers, params=query, json=body)
        r.raise_for_status()
        # Semaphore answers PUT/DELETE with 204 No Content (empty body). Parsing
        # that as JSON blows up ("Expecting value"), so report the success it is.
        if r.status_code == 204 or not r.content:
            return {"ok": True}
        ct = r.headers.get("content-type", "")
        return r.json() if "json" in ct else r.text
'''


def path_params(path):
    return re.findall(r"{(\w+)}", path)


def emit_tool(entry):
    name, method, path, kind = entry["name"], entry["method"], entry["path"], entry["kind"]
    pp = path_params(path)
    body_fields = entry.get("body", [])

    read_only = kind == "read"
    destructive = kind == "delete"
    idempotent = kind in ("read", "delete")
    ann = (f'ToolAnnotations(title={name!r}, readOnlyHint={read_only}, '
           f'destructiveHint={destructive}, idempotentHint={idempotent}, openWorldHint=False)')

    # --- signature: path params (required ints), then body fields, then extras
    args = [f"{p}: int" for p in pp]
    required = [(a, k, t) for a, k, t, d in body_fields if d is REQ]
    optional = [(a, k, t, d) for a, k, t, d in body_fields if d is not REQ]
    args += [f"{a}: {t}" for a, k, t in required]
    args += [f"{a}: {t} = {d}" for a, k, t, d in optional]
    if kind == "read" and name.startswith("list_"):
        args += ['sort: str = "name"', 'order: str = "asc"']
    if kind == "delete":
        args += ["confirm: bool = False"]

    # id injections shared by write bodies: project_id + any trailing {*_id}->"id"
    inject = []
    id_param = None
    for p in pp:
        if p == "project_id":
            inject.append('"project_id": project_id')
        elif p.endswith("_id"):
            inject.append(f'"id": {p}')
            id_param = p

    doc = f"{method} {path}"
    lines = [
        f"@mcp.tool(annotations={ann})",
        f"async def {name}({', '.join(args)}) -> str:",
        f'    """{doc}"""',
    ]

    if kind == "delete":
        lines.append('    if not confirm:')
        lines.append('        return json.dumps({"error": "destructive operation refused", '
                     f'"tool": {name!r}, "hint": "re-invoke with confirm=true to proceed"}}, indent=2)')

    if entry.get("update"):
        # read-modify-write: fetch current, overlay only the fields supplied,
        # re-assert ids, drop write-only echoes, then PUT the merged object.
        strip = entry.get("strip", [])
        updates = ", ".join(f'"{k}": {a}' for a, k, t, d in optional)
        lines.append(f'    current = await _request("GET", f"{path}", query=None, body=None)')
        lines.append('    if not isinstance(current, dict) or current.get("error"):')
        lines.append('        return json.dumps(current, indent=2, default=str)')
        lines.append("    updates = {" + updates + "}")
        lines.append("    body = {**current, **{k: v for k, v in updates.items() if v is not None}}")
        for p in pp:
            if p == "project_id":
                lines.append('    body["project_id"] = project_id')
            elif p.endswith("_id"):
                lines.append(f'    body["id"] = {p}')
        for s in strip:
            lines.append(f'    body.pop({s!r}, None)')
        lines.append(f'    res = await _request("{method}", f"{path}", query=None, body=body)')
    else:
        query_expr = "None"
        body_expr = "None"
        if kind == "read" and name.startswith("list_"):
            query_expr = '{"sort": sort, "order": order}'
        if body_fields or (kind == "write" and pp):
            pairs = inject + [f'"{k}": {a}' for a, k, t in required] \
                           + [f'"{k}": {a}' for a, k, t, d in optional]
            lines.append("    body = {" + ", ".join(pairs) + "}")
            lines.append("    body = {k: v for k, v in body.items() if v is not None}")
            body_expr = "body"
        lines.append(f'    res = await _request("{method}", f"{path}", '
                     f'query={query_expr}, body={body_expr})')

    lines.append("    return json.dumps(res, indent=2, default=str)")
    return "\n".join(lines)


def main():
    spec = yaml.safe_load(open(SPEC))
    known = {(m.upper(), p) for p, item in spec["paths"].items()
             for m in item if m in ("get", "post", "put", "delete")}
    out = [HEADER]
    tiers = {"read": 0, "write": 0, "delete": 0}
    for entry in ALLOWLIST:
        key = (entry["method"], entry["path"])
        assert key in known, f"{entry['method']} {entry['path']} not in spec!"
        out.append("\n\n" + emit_tool(entry))
        tiers[entry["kind"]] += 1

    footer = f'''


def _list():
    tools = asyncio.run(mcp.list_tools())
    for t in tools:
        a = t.annotations
        ro = getattr(a, "readOnlyHint", None) if a else None
        de = getattr(a, "destructiveHint", None) if a else None
        print(f"{{t.name:20}} readOnly={{ro}} destructive={{de}}  {{t.description}}")
    print(f"\\n{{len(tools)}} tools exposed "
          "({tiers['read']} read / {tiers['write']} write / {tiers['delete']} delete-guarded)")


if __name__ == "__main__":
    if "--list" in sys.argv:
        _list()
    else:
        mcp.run()
'''
    out.append(footer)
    open(OUT, "w").write("".join(out))
    total = sum(tiers.values())
    print(f"wrote {OUT}: {total} curated tools "
          f"({tiers['read']} read / {tiers['write']} write / {tiers['delete']} delete-guarded) "
          f"from {len(known)} spec operations")


if __name__ == "__main__":
    main()
