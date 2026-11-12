---
name: nixbot-cli
description: Inspect and control nixbot CI builds with nbo. Use to find the build for a branch/PR, watch it, list failed attributes, fetch failure logs, restart or cancel builds.
---

Server, first match: `NIXBOT_URL`; the `~/.config/nixbot/hosts.toml` entry whose
`remotes` cover the `-R` repo; `git config nixbot.url`; the only hosts.toml
entry; the entry whose `remotes` match `origin`. `-R` alone does not pick a
server unless a `remotes` pattern covers it, so outside the repo's checkout set
`NIXBOT_URL`, for example to the host of a PR's nixbot check link. Check with
`nbo auth status`.

In a checkout, repo and build number are inferred from `origin`/`HEAD`. `origin`
may be a fork, so pass `-R [forge/]owner/name` and an explicit build number.
Reads need no token on public instances.

```bash
nbo build list --branch main
nbo build view 412                           # status + failed attributes
nbo build watch 412 [--attr treefmt]         # exit 1 on failure
nbo log 412                                  # failure summary with log tails
nbo log 412 checks.x86_64-linux.foo --tail 200   # or --follow
nbo log 412 --effect deploy [--follow]       # an effect's log; exit 1 if it failed
nbo log --effect-run 9185                    # any effect run by id, e.g. scheduled
nbo build restart 412 --attr treefmt         # token: NIXBOT_TOKEN / hosts.toml
nbo build restart 412 --effects|--effect deploy  # all or one (repeatable)
nbo build cancel 412 [--effects|--effect deploy]
nbo effects list | run default.deploy        # local, no token
```

Attribute and effect args accept unambiguous substrings; `--json [fields]` for
machine-readable output. HTTP API docs at `/llms.txt`.
