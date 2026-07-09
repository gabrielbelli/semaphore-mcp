# semaphore-mcp

[![CI](https://github.com/gabrielbelli/semaphore-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/gabrielbelli/semaphore-mcp/actions/workflows/ci.yml)
[![ghcr.io](https://img.shields.io/badge/ghcr.io-gabrielbelli%2Fsemaphore--mcp-blue)](https://github.com/gabrielbelli/semaphore-mcp/pkgs/container/semaphore-mcp)

A curated [MCP](https://modelcontextprotocol.io) server for [Semaphore UI](https://semaphoreui.com) — drive your Ansible / Terraform automation from Claude with a **safe, tiered** tool surface: read freely, write deliberately, delete only with an explicit confirmation.

The token never passes through Claude's context — it's an environment variable on your machine. The exposed tool set is generated from Semaphore's own OpenAPI spec and **guardrails deletes** rather than pretending they don't exist.

---

## Why generated, and why curated

Semaphore's API is **127 operations** and ships **empty `operationId`s**, so a generic OpenAPI→MCP bridge produces unusable tool names.

`generate.py` solves that: it reads the pinned `api-docs.yml`, filters to a hand-picked **allowlist**, synthesises clean tool names, and derives `readOnlyHint` / `destructiveHint` annotations from each tool's declared tier. The result is **27 tools across three tiers**:

| Tier | Count | `readOnlyHint` | `destructiveHint` | Guard |
|---|---|---|---|---|
| **read** | 12 | `True` | `False` | safe to auto-allow |
| **write** | 10 | `False` | `False` | `ask` — state-changing, non-destructive |
| **delete** | 5 | `False` | `True` | `ask` **+** refuses unless `confirm=True` |

Covered resources: projects, templates, tasks, inventories, environments (vars), and **repositories** (create/update/delete — needed for branch-pinned testing).

```
GET    /project/{id}/templates              -> list_templates      (read)
GET    /project/{id}/repositories           -> list_repositories   (read)
...
POST   /project/{id}/tasks                  -> run_task            (write)
POST   /project/{id}/templates              -> create_template     (write)
PUT    /project/{id}/environment/{eid}      -> update_environment  (write — set vars)
POST   /project/{id}/repositories           -> create_repository   (write — pin a branch)
...
DELETE /project/{id}/templates/{tid}        -> delete_template     (delete, guarded)
DELETE /project/{id}/tasks/{tid}            -> delete_task         (delete, guarded)
```

Two safety properties worth calling out:

- **Deletes refuse on the first call.** Every `delete_*` returns a refusal payload unless invoked with `confirm=True`, so a stray delete can never fire by accident.
- **Updates never blank what you didn't touch.** `update_*` tools are read-modify-write: they `GET` the current object, overlay only the fields you pass, then `PUT` the merged result — so renaming a template can't silently wipe its arguments or survey vars. (Environment secrets are write-only in the API and are never echoed back; omitting them leaves stored secrets intact.)

Widen or narrow the surface by editing the `ALLOWLIST` in `generate.py` and regenerating.

---

## Quick start (Docker)

Pull the pre-built image from GitHub Container Registry:

```bash
docker pull ghcr.io/gabrielbelli/semaphore-mcp:latest
```

Mint a **scoped API token** in Semaphore (User settings → API Tokens) on an RBAC user scoped to match the tier you intend to allow — the token is the last, hardest fence. Then register with Claude Code by adding this to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "semaphore": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "SEMAPHORE_URL=https://semaphore.example.com",
        "-e", "SEMAPHORE_TOKEN=your-scoped-token",
        "ghcr.io/gabrielbelli/semaphore-mcp:latest"
      ]
    }
  }
}
```

`-i` (not `-t`) is required for stdio MCP transport. Restart Claude Code, then call `list_projects()` to verify.

> **First publish:** GHCR images inherit the source repository's visibility *only after* the image is linked. The `org.opencontainers.image.source` label is set automatically by the workflow; on first publish, visit *Your profile → Packages → semaphore-mcp → Package settings* and either link it to the repo or flip visibility to public.

---

## Four gates against a destructive call

| Layer | Enforced by | Real fence? |
|---|---|---|
| `destructiveHint` / `readOnlyHint` annotations | MCP client (advisory) | hint only |
| `confirm=True` gate on every `delete_*` | the server itself (in-process) | ✅ |
| Claude Code `ask` / `deny` rules | Claude Code permissions | ✅ |
| Scoped Semaphore API token | Semaphore RBAC (returns `403`) | ✅✅ |

Belt-and-braces permission block for Claude Code `settings.json` — reads run silent, writes prompt, deletes are denied outright at the client (drop them into `ask` instead if you want to allow them):

```json
{
  "permissions": {
    "allow": [
      "mcp__semaphore__list_projects", "mcp__semaphore__get_project",
      "mcp__semaphore__list_templates", "mcp__semaphore__get_template",
      "mcp__semaphore__list_tasks", "mcp__semaphore__get_last_tasks",
      "mcp__semaphore__get_task", "mcp__semaphore__get_task_output",
      "mcp__semaphore__list_inventory", "mcp__semaphore__get_inventory",
      "mcp__semaphore__list_environment", "mcp__semaphore__list_repositories"
    ],
    "ask": [
      "mcp__semaphore__run_task", "mcp__semaphore__stop_task",
      "mcp__semaphore__create_template", "mcp__semaphore__update_template",
      "mcp__semaphore__create_environment", "mcp__semaphore__update_environment",
      "mcp__semaphore__create_inventory", "mcp__semaphore__update_inventory",
      "mcp__semaphore__create_repository", "mcp__semaphore__update_repository"
    ],
    "deny": [
      "mcp__semaphore__delete_template", "mcp__semaphore__delete_environment",
      "mcp__semaphore__delete_inventory", "mcp__semaphore__delete_repository",
      "mcp__semaphore__delete_task"
    ]
  }
}
```

---

## Local development

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python generate.py          # api-docs.yml -> server.py
./.venv/bin/python server.py --list     # list tools + annotations (offline)
./.venv/bin/pytest -q                    # safety-surface tests (offline)
```

Or via `make`: `make list`, `make build`, `make test`, `make run`.

### Bumping the pinned spec on a Semaphore upgrade

```bash
curl -sL https://raw.githubusercontent.com/semaphoreui/semaphore/develop/api-docs.yml -o api-docs.yml
python3 generate.py && git diff --stat
```

---

## Licence

BSD 2-Clause. See [LICENSE](LICENSE).
