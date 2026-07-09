# semaphore-mcp

[![CI](https://github.com/gabrielbelli/semaphore-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/gabrielbelli/semaphore-mcp/actions/workflows/ci.yml)
[![ghcr.io](https://img.shields.io/badge/ghcr.io-gabrielbelli%2Fsemaphore--mcp-blue)](https://github.com/gabrielbelli/semaphore-mcp/pkgs/container/semaphore-mcp)

A curated [MCP](https://modelcontextprotocol.io) server for [Semaphore UI](https://semaphoreui.com) — drive your Ansible / Terraform automation from Claude with a **safe, read-first** tool surface.

The token never passes through Claude's context — it's an environment variable on your machine. The exposed tool set is generated from Semaphore's own OpenAPI spec and deliberately **omits every destructive operation**.

---

## Why generated, and why curated

Semaphore's API is **127 operations, 38 of them destructive** (`DELETE`/`PUT`). It also ships **empty `operationId`s**, so a generic OpenAPI→MCP bridge produces unusable tool names.

`generate.py` solves both: it reads the pinned `api-docs.yml`, filters to a hand-picked **allowlist**, synthesises clean tool names, and derives `readOnlyHint` / `destructiveHint` annotations from the HTTP verb. The result is **13 tools** — 11 read-only, 2 guarded writes, zero deletes.

```
GET  /projects                              -> list_projects       (readOnly)
GET  /project/{id}/templates                -> list_templates      (readOnly)
GET  /project/{id}/tasks                    -> list_tasks          (readOnly)
GET  /project/{id}/tasks/{tid}              -> get_task            (readOnly)
GET  /project/{id}/tasks/{tid}/output       -> get_task_output     (readOnly)
...
POST /project/{id}/tasks                    -> run_task            (write)
POST /project/{id}/tasks/{tid}/stop         -> stop_task           (write)
```

Widen or narrow the surface by editing the `ALLOWLIST` in `generate.py` and regenerating.

---

## Quick start (Docker)

Pull the pre-built image from GitHub Container Registry:

```bash
docker pull ghcr.io/gabrielbelli/semaphore-mcp:latest
```

Mint a **scoped API token** in Semaphore (User settings → API Tokens) on an RBAC user with read + task-run permissions only. Then register with Claude Code by adding this to `~/.claude/mcp.json`:

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
| Destructive tools not generated | `generate.py` allowlist | ✅ |
| Claude Code `ask` / `deny` rules | Claude Code permissions | ✅ |
| Scoped Semaphore API token | Semaphore RBAC (returns `403`) | ✅✅ |

Belt-and-braces permission block for Claude Code `settings.json` — reads run silent, writes prompt every time:

```json
{
  "permissions": {
    "allow": [
      "mcp__semaphore__list_projects", "mcp__semaphore__get_project",
      "mcp__semaphore__list_templates", "mcp__semaphore__get_template",
      "mcp__semaphore__list_tasks", "mcp__semaphore__get_last_tasks",
      "mcp__semaphore__get_task", "mcp__semaphore__get_task_output",
      "mcp__semaphore__list_inventory", "mcp__semaphore__get_inventory",
      "mcp__semaphore__list_environment"
    ],
    "ask": ["mcp__semaphore__run_task", "mcp__semaphore__stop_task"]
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
