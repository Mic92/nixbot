---
name: nixbot-cli
description: Inspect and control nixbot CI builds with nbo. Use to find the build for a branch/PR, watch it, list failed attributes, fetch failure logs, restart or cancel builds.
---

Server: `NIXBOT_URL` or `~/.config/nixbot/hosts.toml`. In a checkout, repo and
build number are inferred from `origin`/`HEAD`, override with
`-R [forge/]owner/name` and an explicit build number.

```bash
nbo build list --branch main
nbo build view 412                           # status + failed attributes
nbo build watch 412 [--attr treefmt]         # exit 1 on failure
nbo log 412                                  # failure summary with log tails
nbo log 412 checks.x86_64-linux.foo --tail 200   # or --follow
nbo build restart 412 --attr treefmt         # token: NIXBOT_TOKEN / hosts.toml
nbo build cancel 412
nbo effects list | run default.deploy        # local, no token
```

Attribute args accept unambiguous substrings; `--json [fields]` for
machine-readable output. `nbo auth status` shows server/token. HTTP API docs at
`/llms.txt`.
