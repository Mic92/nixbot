# Command-line client (nbo)

`nbo` lets you inspect and control builds on a nixbot instance from the
terminal: list builds, watch them finish, read failure logs, and restart or
cancel builds.

Run it directly:

```console
nix run github:nix-community/nixbot#nixbot-cli -- --help
```

or add the `nixbot-cli` package to your profile or dev shell to get the `nbo`
command.

## Connecting to a server

Point `nbo` at your instance with the `NIXBOT_URL` environment variable or a
config file:

```console
export NIXBOT_URL=https://ci.example.org
```

```toml
# ~/.config/nixbot/hosts.toml
["https://ci.example.org"]
token = "bnix_..."
```

Read-only commands work without a token on public instances.

### API tokens

Restarting or cancelling builds and enabling repositories require an API token.
Log in to the web UI of your instance and create one under **Settings**
(`https://ci.example.org/settings`), then either put it in `hosts.toml` as shown
above or export it:

```console
export NIXBOT_TOKEN=bnix_...
```

Instead of storing the token in plain text you can have `nbo` fetch it from a
password manager such as `pass` or `rbw` on every run:

```toml
# ~/.config/nixbot/hosts.toml
["https://ci.example.org"]
token_command = "pass show ci.example.org/nixbot"
# or: token_command = "rbw get nixbot-ci"
```

`nbo auth status` shows which server and token are in use and whether the server
is reachable.

## Repositories and builds

Inside a checkout the repository is inferred from the `origin` remote and the
build number from the `HEAD` commit; both can be given explicitly
(`-R [forge/]owner/name`, a build number as first argument).

```console
nbo repo list
nbo repo enable github/acme/widget      # admin token required
nbo build list --branch main
nbo build view 412                      # status, attribute summary, failed attributes
nbo build watch 412                     # follow until it finishes, exit 1 on failure
nbo build watch 412 --attr treefmt --attr nixos-eve   # wait only for these attributes
nbo build restart 412 --attr x86_64-linux.treefmt     # a substring like "treefmt" works too
nbo build restart 412 --effects
nbo build cancel 412
```

`--json [fields]` on list/view/log commands prints machine-readable output,
optionally projected to a comma-separated field list.

## Logs

```console
nbo log 412                                         # failure summary with log tails
nbo log 412 checks.x86_64-linux.nixos-test --tail 200
nbo log 412 /nix/store/...-zfs-2.2.4.drv            # one derivation of the attribute
nbo log 412 checks.x86_64-linux.nixos-test --follow # stream while it runs
```

Attribute arguments accept unambiguous substrings.

## Exit codes

| code | meaning                              |
| ---- | ------------------------------------ |
| 0    | success                              |
| 1    | the build or attribute failed        |
| 2    | usage error / not found              |
| 4    | authentication or permission failure |

Scripts and agents can also use the underlying HTTP API directly: every instance
documents it under `/llms.txt` and `/api/openapi.json`.
