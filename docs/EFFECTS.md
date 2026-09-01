# Hercules CI effects

See [flake.nix](../flake.nix) for an example and the
[Hercules CI effects documentation](https://docs.hercules-ci.com/hercules-ci/effects/)
for the upstream reference.

## Ordering and locks

Effects can declare two attributes on the effect derivation:

- `after`: attribute paths of effects of the same build that must succeed first,
  e.g. `[ [ "default" "build-image" ] ]`. Each entry is a list of strings: the
  onPush job first, then the path inside that job's effects attrset (nested
  effects work too: `[ "default" "env" "staging" ]`), so it matches the dotted
  names shown everywhere else and can reference effects of another job. The
  effect only starts once everything in `after` succeeded; if a dependency
  fails, the effect is marked `skipped` and reported as a failure. Effects not
  ordered against each other run in parallel.
- `lock`: a named lock. Effects holding the same lock name run one at a time per
  project, across builds and pull requests — use it for shared resources like a
  staging environment or a hardware lab. A lock is handed out in build order:
  all of a build's effects on a lock finish before the next build's start, so
  multi-effect deployments never interleave across builds.

Both are ordinary attributes on the effect derivation: set them next to
`effectScript`, whether you write plain attribute sets, use this repo's
[effects-lib](../herculesCI/effects-lib.nix), or upstream
[hercules-ci-effects](https://docs.hercules-ci.com/hercules-ci-effects/)'
`mkEffect` (extra attributes pass through). They are a nixbot extension:
Hercules CI itself ignores them; only nixbot orders and serializes on them.

```nix
# flake.nix
{
  inputs.nixbot.url = "github:Mic92/nixbot";

  outputs = { self, nixpkgs, nixbot, ... }: {
    herculesCI = { primaryRepo, ... }: let
      pkgs = nixpkgs.legacyPackages.x86_64-linux;
      # Or hercules-ci-effects' mkEffect; after/lock pass through there too.
      inherit (nixbot.lib.effects { inherit pkgs; }) mkEffect;
    in {
      onPush.default.outputs.effects = {
        push-image = mkEffect {
          inputs = [ pkgs.skopeo ];
          effectScript = ''
            skopeo copy docker-archive:${self.packages.x86_64-linux.image} \
              docker://registry.example.org/app:${primaryRepo.rev}
          '';
        };

        # Starts only after push-image succeeded; the "staging" lock makes
        # concurrent PRs take turns deploying to staging.
        deploy-staging = mkEffect {
          after = [ [ "default" "push-image" ] ];
          lock = "staging";
          inputs = [ pkgs.kubectl ];
          effectScript = ''
            kubectl set image deployment/app app=registry.example.org/app:${primaryRepo.rev}
          '';
        };

        deploy-prod = mkEffect {
          after = [ [ "default" "deploy-staging" ] ];
          lock = "prod";
          inputs = [ pkgs.kubectl ];
          effectScript = ''
            kubectl --context prod set image deployment/app app=registry.example.org/app:${primaryRepo.rev}
          '';
        };

        # No after/lock: runs in parallel with everything else.
        notify = mkEffect {
          inputs = [ pkgs.curl ];
          effectScript = ''
            curl -sf -d "deployed ${primaryRepo.rev}" https://chat.example.org/hook
          '';
        };
      };
    };
  };
}
```

With this flake, one push runs `push-image` and `notify` immediately and in
parallel, then `deploy-staging`, then `deploy-prod`. If `push-image` fails, both
deploys end up `skipped` and the commit status is red. Cycles or unknown paths
in `after` fail effect discovery.

nixbot evaluates every `onPush.<job>` of the `herculesCI` output. Outside the
flake, effects are addressed by their dotted attribute path prefixed with the
job name: `nbo effects run default.deploy-staging`, log names, and the `after`
entries in `nbo effects list` output. Inspect the DAG locally:

```console
$ nbo effects graph
default.notify
default.push-image
└── default.deploy-staging [lock: staging]
    └── default.deploy-prod [lock: prod]
```

## nixbot.toml Configuration

Effects branch configuration and allowing effects to run in PRs is configured
via nixbot.toml in the default branch and is documented in the
[README.md](../README.md#per-repository-configuration).

## CLI usage

The `nbo effects` commands (part of the [nbo CLI](CLI.md)) can list and run
effects locally, or against remote repositories using
[Nix flake references](https://nix.dev/manual/nix/latest/command-ref/new-cli/nix3-flake#flake-references).

### Local repository

```console
$ cd my-repo
$ nbo effects list
{"default.deploy": {"after": ["default.notify"], "lock": "hw-lab"}, "default.notify": {"after": [], "lock": null}}

$ nbo effects graph
default.notify
└── default.deploy [lock: hw-lab]

$ nbo effects run default.deploy
```

### Remote repository (flake reference)

No local checkout needed:

```console
$ nbo effects run github:org/repo/branch#default.deploy
$ nbo effects list github:org/repo/branch
$ nbo effects list-schedules github:org/repo/branch
$ nbo effects run-scheduled github:org/repo#flake-update update
```

### Subcommands

| Command          | Description                           |
| ---------------- | ------------------------------------- |
| `list`           | List available effects with metadata  |
| `graph`          | Show the effect DAG as an ASCII tree  |
| `run`            | Run a single effect                   |
| `list-schedules` | List scheduled effects                |
| `run-scheduled`  | Run a specific effect from a schedule |

### Flags

All subcommands accept:

| Flag           | Description                                                                                                                      |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `--rev`        | Git revision to use                                                                                                              |
| `--branch`     | Git branch to use                                                                                                                |
| `--repo`       | Git repo name                                                                                                                    |
| `--path`       | Path to the repository (default: current directory)                                                                              |
| `--debug`      | Enable debug mode (may leak secrets such as GITHUB_TOKEN)                                                                        |
| `--no-refresh` | Do not pass `--refresh` when resolving flake references (refresh is on by default so remote refs resolve to the latest revision) |

`run` and `run-scheduled` also accept:

| Flag        | Description                      |
| ----------- | -------------------------------- |
| `--secrets` | Path to a JSON file with secrets |

## Running effects locally with secrets

Pass `--secrets` to provide secrets when running effects locally. The file is a
JSON object where each key is a secret name and its value has a `"data"` field
containing key-value pairs:

```json
{
  "my-secret": {
    "data": {
      "token": "ghp_xxxxxxxxxxxx",
      "username": "deploy-bot"
    }
  }
}
```

```console
$ nbo effects run --secrets secrets.json default.deploy
```

Inside the effect, secrets are available at `/run/secrets.json` (via
`HERCULES_CI_SECRETS_JSON`). This follows the
[hercules-ci secrets format](https://docs.hercules-ci.com/hercules-ci-agent/secrets-json/).

## Pushable repository checkout

Effects that modify the repository (auto-updates, formatting bots) can ask
nixbot for a ready-made working copy instead of cloning inside the sandbox:

```nix
effects.flake-update = mkEffect {
  checkout = true;
  effectScript = ''
    nix flake update
    git commit -am "flake.lock: update"
    git push origin HEAD:update-flake-lock
  '';
};
```

nixbot clones the repository from its local mirror at the commit the effect runs
for and mounts it writable at `/build/checkout`, which is also the effect's
working directory (and exported as `NIXBOT_EFFECT_CHECKOUT`). The clone's
`origin` uses the forge token, so `git push` works without extra secrets. The
clone is removed after the effect finishes.

The `checkout` argument of `mkEffect` sets the `__nixbot_effect_checkout`
derivation attribute; effects built without `mkEffect` can set the attribute
directly. Effects that do not set it get no checkout. If the repository has no
forge token to push with, the effect fails with an error.

## Event effects (`onEvent`)

`herculesCI.onEvent.<kind>.<name>` defines effects that react to pull requests,
PR comments, PR close and build results. See
[examples/on-event/flake.nix](../examples/on-event/flake.nix). Hercules CI
ignores `onEvent`.

The effect code is **always evaluated from the default branch**. A pull request
cannot change what runs by editing `onEvent`. This is independent of
`effects_on_pull_requests`, which controls whether a PR's own `onPush` effects
run. The event only contributes data, available at runtime as JSON in
`$NIXBOT_EVENT_JSON` (`/run/event.json`) and as `NIXBOT_EVENT_KIND`,
`NIXBOT_ACTOR`, `NIXBOT_PR_NUMBER`, `NIXBOT_PR_HEAD`, `NIXBOT_COMMAND`,
`NIXBOT_COMMAND_ARGS`, `NIXBOT_BUILD_STATUS`, `NIXBOT_BUILD_URL`.

| kind                  | delivered when                                     | payload                                                                  |
| --------------------- | -------------------------------------------------- | ------------------------------------------------------------------------ |
| `pull_request`        | a PR build finished `succeeded`                    | `actor?`, `build`, `pullRequest`                                         |
| `pull_request_closed` | PR closed or merged                                | `actor?`, `pullRequest` (with `merged`)                                  |
| `comment`             | new PR comment whose first line is `/command args` | `actor`, `command`, `args`, `pullRequest`, `build?`                      |
| `build_finished`      | any build reached a terminal status                | `actor?`, `build` (with `previousStatus`, `failedAttrs`), `pullRequest?` |

`actor` is who caused the delivery (pusher, commenter, the user who pressed
restart) as `{ name = "github:alice"; permission = "write"; }`. It is absent
when nobody did (poller). `pullRequest.author` carries the PR opener the same
way. Permissions are looked up on the forge at delivery time.

`mkEffect { when = { ... }; }` restricts when an effect runs. All given keys
must match. An effect that does not match is recorded as `skipped` with the
reason shown in the web UI.

| `when` key                              | matches if                                                        |
| --------------------------------------- | ----------------------------------------------------------------- |
| `permission = "read"\|"write"\|"admin"` | actor **or** PR author has at least this level                    |
| `labels = [ ... ]`                      | PR has all of these labels                                        |
| `branches = [ "main" "release-*" ]`     | glob on PR base branch, or build branch                           |
| `commands = [ "plan" ]`                 | `comment` kind: the `/command` used                               |
| `status = [ "succeeded" ]`              | status of the build in the payload                                |
| `transition = "broke"\|"fixed"`         | build status vs the previous finished build of the same branch/PR |

`lock` works as for `onPush` and may contain `{pr}`, so `lock = "preview-{pr}"`
serialises per pull request, also against `onPush` effects using the same
expanded name. A newer delivery for the same PR cancels still-queued effects of
the previous one. `after` is not supported for event effects.

`checkout = true` mounts the **PR head** at `/build/checkout`. Unlike for
`onPush` the clone has no push credentials, and its content is untrusted: tools
like `tofu plan` execute code from it, so guard such effects with
`when.permission`.

Try an effect locally by describing the event with flags instead of pushing:

```console
$ nbo effects list --event pull_request --pr 7 --permission write --label preview
$ nbo effects run --event comment --command plan --actor github:alice \
    --permission write plan
```

`list` prints every effect of that kind together with the reason it would be
skipped. The flags cover what `when` looks at: `--pr`, `--actor`,
`--permission`, `--author-permission`, `--label`, `--command`, `--args`,
`--build-status` and `--previous-build-status`. `--payload FILE` takes a
complete payload instead.

## Buildbot secrets configuration

When running effects through nixbot (not locally), secrets are configured at
different scopes:

1. **Repository-specific**: `"github:owner/repo"` — applies to a single
   repository
2. **Organization-wide**: `"github:org/*"` — applies to all repositories in an
   organization

```nix
services.nixbot.effects.perRepoSecretFiles = {
  # All repos in nix-community org get this token
  "github:nix-community/*" = config.agenix.secrets.nix-community-effects.path;

  # This specific repo gets its own token (overrides org-level)
  "github:nix-community/nixbot" = config.agenix.secrets.nixbot-effects.path;

  # All repos in a Gitea org
  "gitea:my-org/*" = config.agenix.secrets.my-org-effects.path;
};
```

The secrets files must be valid JSON files containing the secrets that will be
made available to your effects at runtime.
