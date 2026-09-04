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

| Command          | Description                                                         |
| ---------------- | ------------------------------------------------------------------- |
| `list`           | List effects with metadata (`--event KIND --payload F` for onEvent) |
| `graph`          | Show the effect DAG as an ASCII tree                                |
| `run`            | Run a single effect (`--event`/`--payload` likewise)                |
| `list-schedules` | List scheduled effects                                              |
| `run-scheduled`  | Run a specific effect from a schedule                               |

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

`onPush` effects run when a branch is built. `onEvent` effects react to things
happening around a build: a pull request turning green, a `/command` comment, a
PR being closed, a build breaking or getting fixed. A typical use is `tofu plan`
on every PR with the result posted as a comment, and `tofu apply` on merge. A
complete example is in
[examples/on-event/flake.nix](../examples/on-event/flake.nix); Hercules CI
itself ignores `onEvent`.

```nix
herculesCI = { ... }: {
  onPush.default.outputs.effects.apply = mkEffect {
    lock = "infra";
    effectScript = "tofu apply -auto-approve";
  };
  onEvent.pull_request.plan = mkEffect {
    when.permission = "write";   # pusher or PR author can write to the repo
    lock = "infra";              # waits for a running apply
    checkout = true;             # PR head at $NIXBOT_EFFECT_CHECKOUT
    effectScript = ''
      tofu plan -no-color | nixbot-pr-comment --replace-marker plan
    '';
  };
  onEvent.comment.apply = mkEffect {
    when = { commands = [ "apply" ]; permission = "admin"; };
    lock = "infra";
    checkout = true;
    effectScript = "tofu apply -auto-approve $NIXBOT_COMMAND_ARGS";
  };
};
```

### Where the code comes from

The effect definitions are **always evaluated from the default branch**, no
matter which pull request the event is about. A PR cannot change what runs by
editing `onEvent`; it only contributes data. This is unrelated to
`effects_on_pull_requests` in nixbot.toml, which is about a PR's own `onPush`
effects.

With `checkout = true` the **PR head** is cloned to `/build/checkout` (the
working directory, also `$NIXBOT_EFFECT_CHECKOUT`). Unlike the `onPush` checkout
it has no push credentials, and its content is untrusted: `tofu plan` and
similar tools execute code from it. Guard such effects with `when.permission`.
Event effects otherwise get the same secrets and forge token as `onPush`
effects.

### Events

| kind                  | delivered when                                              |
| --------------------- | ----------------------------------------------------------- |
| `pull_request`        | a PR head was built green; again when reopened or relabeled |
| `comment`             | a PR comment whose first line is `/command args`            |
| `pull_request_closed` | a PR was closed or merged                                   |
| `build_finished`      | any build finished                                          |

Comments by bots (GitHub apps, nixbot's own account on Gitea/GitLab) are
ignored. A `/command` for an effect that is still running from an earlier
comment is answered with a note instead of being queued twice. Deliveries are
queued in the database and retried with backoff while the forge API is
unavailable; webhooks sent while nixbot itself is down are not replayed.

The event is passed to the script as JSON in `$NIXBOT_EVENT_JSON`
(`/run/event.json`) and, for the common fields, as environment variables:
`NIXBOT_EVENT_KIND`, `NIXBOT_ACTOR`, `NIXBOT_PR_NUMBER`, `NIXBOT_PR_HEAD`,
`NIXBOT_COMMAND`, `NIXBOT_COMMAND_ARGS`, `NIXBOT_BUILD_STATUS`,
`NIXBOT_BUILD_URL`. The JSON has up to four top-level keys:

- `actor`: who caused it, `{ "name": "github:alice", "permission": "write" }`.
  Absent when nobody did (polled changes).
- `pullRequest`: `number`, `title`, `url`, `author` (shaped like `actor`),
  `baseRef`, `headRef`, `headRev`, `labels`, `draft`, `isFork`, `merged`.
- `build`: `number`, `url`, `status`, `branch`, `rev`; for `build_finished` also
  `previousStatus` and `failedAttrs`.
- `command`, `args`: for `comment`.

Permissions are looked up on the forge when the event is delivered.

### Conditions

`when` restricts an effect to some deliveries. All given keys must match; an
effect that does not match is listed as skipped with the reason.

| `when` key                          | matches if                                                                                      |
| ----------------------------------- | ----------------------------------------------------------------------------------------------- |
| `permission = "write"`              | actor or PR author has at least `read`/`write`/`admin`; for `comment` only the commenter counts |
| `labels = [ "deploy" ]`             | the PR has all of these labels                                                                  |
| `branches = [ "main" "release-*" ]` | glob on the PR base branch, or the built branch                                                 |
| `commands = [ "plan" ]`             | `comment`: the `/command` used                                                                  |
| `modified = [ "terraform/*" ]`      | a file the PR changes matches a glob; pull request events only                                  |
| `status = [ "failed" ]`             | status of the build in the payload                                                              |
| `transition = "broke"` / `"fixed"`  | build status changed against the previous finished build of that branch or PR                   |

### Ordering

`lock` works as for `onPush`, so an event effect and a deploy holding the same
lock never overlap. The name may contain `{pr}`: `lock = "preview-{pr}"`
serialises per pull request. A newer delivery of the same kind for the same PR
(for comments: the same command) cancels effects of the previous one that have
not started yet. `after` is not supported.

### Reporting back

Event effects post no commit statuses. Instead `mkEffect` puts
`nixbot-pr-comment` on `PATH`, which comments on the pull request the event is
about: `nixbot-pr-comment "text"` or `... | nixbot-pr-comment`. With
`--replace-marker ID` the comment a previous run left with the same marker is
edited instead of adding another. Run locally it just prints the body.

### Checks on every build

A pull request that breaks an effect should be red before it is merged, even
though its effects do not run. Every build therefore evaluates `onPush` and
`onEvent` of the built commit and builds each effect's dependencies without
running it (stdenv's `inputDerivation`, or `dependencies` of a hercules-ci
`runIf false` effect). The results appear as "Effect checks" on the build page
and count towards the `effects` forge status; evaluation errors are shown there
too. If `onEvent` on the default branch is broken, the error is shown on the
build an event was delivered for.

### Restarting and testing

Event effects can be restarted from the build and run pages, with
`nbo build restart N --effect comment/apply`, or
`POST /api/repos/.../builds/N/effects/restart?name=apply&kind=comment`. A
running one is cancelled first; the stored payload is reused.

To try an effect locally, write the payload by hand:

```console
$ nbo effects list --event pull_request --payload pr.json
$ nbo effects run --event comment --payload comment.json --effect-checkout . apply
```

## Secrets on the server

When nixbot runs effects, secrets come from files configured per repository or
per organization:

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
