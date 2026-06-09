# Security Policy

## Supported versions

| Component | Supported |
|-----------|-----------|
| Latest release tag | ✅ |
| Older tags | ❌ |

Security fixes are applied to the current release only. Pin to the latest tag.

---

## Threat model

SerenMemory is a **self-hosted** service. Nothing is sent to a third party - all memory is stored in a local [ChromaDB](https://www.trychroma.com/) database that you control.

The relevant attack surface is:

| Surface | Default | Notes |
|---------|---------|-------|
| HTTP API | `127.0.0.1:7420` | Localhost only by default. Exposing on `0.0.0.0` puts it on the network - use bearer auth and a reverse proxy if you do. |
| Bearer token | Not set | Optional but strongly recommended for any non-localhost bind. Token is stored in `seren-memory.yaml` - the setup scripts lock file permissions on creation. |
| MCP endpoint (`/mcp/`) | Same host/port as HTTP API | Subject to the same bearer auth middleware. |
| Memory viewer (`/viewer`) | Public (loads before auth prompt) | The viewer page itself is public so the token input can render; all data API calls require the bearer token. |
| Config file | `~/seren-memory/seren-memory.yaml` | May contain the bearer token. Setup scripts set `0600` (Unix) or ACL-lock to the current user (Windows). Do not commit this file. |
| ChromaDB data | `~/.seren-memory/chroma/` | Local filesystem. Back it up; it is your memory. |

---

## Deployment recommendations

- **Local use**: default bind (`127.0.0.1`) with no token is fine.
- **Team / LAN use**: bind to a specific interface, enable a bearer token, and put a TLS-terminating reverse proxy (nginx, Caddy) in front. Never expose the raw HTTP port to untrusted networks.
- **Locked-down / air-gapped environments**: no consolidator model is required. Leave `model_url` blank and Copilot manages briefs and drafts via MCP. No outbound model calls are made.

---

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Open a [GitHub Security Advisory](https://github.com/ChadRoesler/SerenMemory/security/advisories/new) (private disclosure). Include:

- A description of the issue and its impact
- Steps to reproduce
- Any relevant config or environment details

You will get a response within **7 days**. If a fix is needed, a patched release will be tagged and the advisory will be published after users have had time to update.
