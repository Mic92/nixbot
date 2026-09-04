# Command-line client (nbo)

`nbo` lets you inspect and control builds on a nixbot instance from the
terminal: list builds, watch them finish, read failure logs, and restart or
cancel builds.

Run it directly:

```console
nix run github:Mic92/nixbot#nixbot-cli -- --help
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

If you use more than one nixbot server, tell `nbo` which repositories belong to
which server. Add a `remotes` list to each server entry. When you run `nbo`
inside a checkout, it looks at the URL of the `origin` remote and picks the
first server whose pattern matches:

```toml
["https://ci.example.org"]
token = "bnix_..."
remotes = ["github.com/acme/*"]

["https://ci.other.org"]
remotes = ["git.other.org/*"]
```

Patterns are shell-style wildcards matched against the remote URL in the form
`host/owner/repo`, so both `git@github.com:acme/widget.git` and
`https://github.com/acme/widget` match `github.com/acme/*`.

You can also assign a single checkout to a server directly, which takes
precedence over the patterns:

```console
git config nixbot.url https://ci.example.org
```

When several settings are present, `nbo` uses the first of:

1. the `NIXBOT_URL` environment variable
2. `git config nixbot.url`
3. a `remotes` pattern match
4. the only server in `hosts.toml`, if there is just one

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
nbo repo enable github/acme/widget      # instance admin or forge repo admin
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

## Effects

`nbo effects` runs and inspects a flake's [effects](EFFECTS.md) locally with nix
(no API token needed):

```console
nbo effects list                        # effects with their after/lock metadata
nbo effects graph                       # dependency DAG as an ASCII tree
nbo effects run default.deploy          # run one effect in the local sandbox
nbo effects run github:org/repo/branch#default.deploy
nbo effects run-scheduled github:org/repo#nightly flake-update
nbo effects list --event pull_request --pr 7 --permission write
nbo effects run --event comment --command apply --permission admin apply
```

For `onEvent` effects, `--event KIND` plus flags describing the event (`--pr`,
`--actor`, `--permission`, `--label`, `--modified`, `--command`, ...) stand in
for a real delivery, and `list` then shows why each effect would be skipped.

## Exit codes

| code | meaning                              |
| ---- | ------------------------------------ |
| 0    | success                              |
| 1    | the build or attribute failed        |
| 2    | usage error / not found              |
| 4    | authentication or permission failure |

Scripts and agents can also use the underlying HTTP API directly: every instance
documents it under `/llms.txt` and `/api/openapi.json`.
