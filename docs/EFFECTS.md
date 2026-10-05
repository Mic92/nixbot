# Effects

An effect is a script that changes something outside the build: pushing an
image, deploying a machine, publishing a release, commenting on a pull request.
nixbot builds everything an effect needs first, then runs it in a sandbox with
access to the Nix store, the network and the secrets you configured.

Effects follow
[Hercules CI effects](https://docs.hercules-ci.com/hercules-ci/effects/): a
flake that works there works here, with
[hercules-ci-effects](https://docs.hercules-ci.com/hercules-ci-effects/) as the
library; `mkEffect` is also available from nixbot's
[effects-lib](../herculesCI/effects-lib.nix). nixbot adds ordering and locks,
skipping unchanged effects, tag and event effects, and the `nbo effects`
command.

## Your first effect

Effects live in the `herculesCI` output of your flake. Every `onPush.<job>` is
evaluated, and the attributes of its `outputs.effects` are the effects:

```nix
# flake.nix
{
  inputs.nixbot.url = "github:Mic92/nixbot";

  outputs = { self, nixpkgs, nixbot, ... }: {
    herculesCI = { primaryRepo, ... }: let
      pkgs = nixpkgs.legacyPackages.x86_64-linux;
      inherit (nixbot.lib.effects { inherit pkgs; }) mkEffect;
    in {
      onPush.default.outputs.effects.notify = mkEffect {
        inputs = [ pkgs.curl ];
        effectScript = ''
          curl -sf -d "built ${primaryRepo.rev}" https://chat.example.org/hook
        '';
      };
    };
  };
}
```

Push this to the default branch. Once the build is green, `notify` runs and its
log appears on the build page, next to the build's other results. The `effects`
commit status shows whether all effects succeeded.

Try it before pushing with `nbo effects run default.notify`, see
[Running effects locally](#running-effects-locally).

## When effects run

- A push to the default branch runs its `onPush` effects after the build
  succeeded. Other branches and pull requests only build the effects'
  dependencies and report problems as "Effect checks", see
  [Checks on every build](#checks-on-every-build).
- `effects_branches` and `effects_on_pull_requests` in `nixbot.toml` allow
  effects on more branches or on pull requests. nixbot reads them from the
  default branch, so a pull request cannot enable itself. See
  [Per Repository Configuration](../README.md#per-repository-configuration).
  Pull request effects get the same secrets, so only enable them for
  repositories you trust all contributors of.
- Pushing a tag runs the effects too, see [Tag pushes](#tag-pushes).
- [Event effects](#event-effects) run when a pull request turns green, a comment
  asks for it, and similar events.

A build's effects start after all its builds succeeded and run in parallel,
unless you [order them](#ordering-effects).

## Running effects locally

`nbo effects` (part of the [nbo CLI](CLI.md#effects)) lists, graphs and runs
effects without pushing, also for a remote flake reference:

```console
$ nbo effects list
$ nbo effects graph
$ nbo effects run default.deploy
$ nbo effects run github:org/repo/branch#default.deploy
```

Effects are named by their `onPush` job and attribute path, `default.deploy` for
`onPush.default.outputs.effects.deploy`. For secrets see
[Running locally with secrets](#running-locally-with-secrets). `--secrets FILE`
and `nbo effects --help` list the remaining flags.

## Ordering effects

Two attributes on an effect control when it runs:

- `after`: effects of the same build that must succeed first, each as
  `[ "<job>" "<attribute>" ... ]`, e.g. `[ [ "default" "push-image" ] ]`. If a
  dependency fails, the effect is `skipped` and the build is reported as failed.
  Effects not ordered against each other run in parallel. Cycles or unknown
  paths fail effect discovery.
- `lock`: a named lock. Effects holding the same lock run one at a time per
  project, across builds and pull requests, which suits a staging environment or
  a hardware lab. A lock is handed out in build order, so the effects of two
  builds never interleave.

Both are plain attributes next to `effectScript`. Hercules CI ignores them.

```nix
herculesCI = { primaryRepo, ... }: {
  onPush.default.outputs.effects = {
    push-image = mkEffect {
      inputs = [ pkgs.skopeo ];
      effectScript = ''
        skopeo copy docker-archive:${self.packages.x86_64-linux.image} \
          docker://registry.example.org/app:${primaryRepo.rev}
      '';
    };

    deploy-staging = mkEffect {
      after = [ [ "default" "push-image" ] ];
      lock = "staging";
      inputs = [ pkgs.kubectl ];
      effectScript = "kubectl set image deployment/app app=registry.example.org/app:${primaryRepo.rev}";
    };

    deploy-prod = mkEffect {
      after = [ [ "default" "deploy-staging" ] ];
      lock = "prod";
      inputs = [ pkgs.kubectl ];
      effectScript = "kubectl --context prod set image deployment/app app=registry.example.org/app:${primaryRepo.rev}";
    };

    # No after or lock: starts immediately.
    notify = mkEffect {
      inputs = [ pkgs.curl ];
      effectScript = ''curl -sf -d "deployed ${primaryRepo.rev}" https://chat.example.org/hook'';
    };
  };
};
```

One push runs `push-image` and `notify` at once, then `deploy-staging`, then
`deploy-prod`. If `push-image` fails, both deploys are skipped and the commit
status is red. `nbo effects graph` shows the result:

```console
$ nbo effects graph
default.notify
default.push-image
└── default.deploy-staging [lock: staging]
    └── default.deploy-prod [lock: prod]
```

## Skipping unchanged effects

`when.changed` names the inputs an effect depends on. The effect is not run
again while they are the same as in its last successful run:

```nix
let
  firmware = self.packages.x86_64-linux.image;
  flasher = pkgs.hello;
in
mkEffect {
  lock = "rig";
  when.changed = { inherit firmware flasher; };
  effectScript = "flash ${firmware} && run-tests";
}
```

Values must be strings, typically store paths: up to 32 inputs of at most 1024
bytes each. When the effect's turn comes (after its `lock`) and the last
successful run of this effect in the repository had the same inputs, it is
marked succeeded as "unchanged since build #N" without running. Effects `after`
it run as usual.

Failed runs and runs for pull requests do not count. A restart always runs the
effect. Only `onPush` effects support `when.changed`, and no other `when` key.

## Secrets and credentials

An effect asks for secrets with `secretsMap`, which maps the name the script
uses to a secret nixbot holds. The sections below configure the secrets, read
them in a script and write the credential files common tools expect.

### Configuring secrets on the server

Secrets are configured per repository or per organization:

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

### Reading secrets

In the shell functions below, a secret `NAME` is a key of the effect's
`secretsMap`, or of the secrets JSON if it has none.

- `readSecretString NAME PATH` prints a string from the secret's `data`. `PATH`
  is a `jq` path. It fails if the path doesn't exist.
- `readSecretJSON NAME PATH` prints any value as compact JSON.

```nix
mkEffect {
  # Secret "cloud": { "data": { "token": "...", "regions": ["eu", "us"] } }
  secretsMap.cloud = "cloud";
  inputs = [ pkgs.curl ];
  effectScript = ''
    token=$(readSecretString cloud .token)
    regions=$(readSecretJSON cloud .regions)   # ["eu","us"]
    curl -H "Authorization: Bearer $token" -d "$regions" https://example.org
  '';
}
```

### Writing credentials

| Function                              | Secret fields                                     | Result                                          |
| ------------------------------------- | ------------------------------------------------- | ----------------------------------------------- |
| `writeSSHKey [NAME=ssh] [FILE]`       | `privateKey`, optional `publicKey`                | `~/.ssh/id_rsa` (mode 0400) and `.pub`          |
| `writeAWSSecret [NAME=aws] [PROFILE]` | `aws_access_key_id`, `aws_secret_access_key`      | profile in `~/.aws/credentials`                 |
| `writeDockerKey [NAME=docker] [DIR]`  | `clientKey`, `clientCertificate`, `CACertificate` | `key.pem`, `cert.pem`, `ca.pem` in `~/.docker`  |
| `writeGPGKey [NAME=gpg]`              | `privateKey`                                      | key imported into the keyring and fully trusted |

`useDockerHost HOST [PORT=2376]` points Docker at a remote host with TLS.

Add the tools these call to `inputs`: `pkgs.openssh` (`ssh-keygen`, used when
the secret has no `publicKey`) and `pkgs.gnupg`.

```nix
mkEffect {
  inputs = [ pkgs.git pkgs.openssh ];
  secretsMap.ssh = "deploy-key";
  effectScript = ''
    writeSSHKey ssh                   # ~/.ssh/id_rsa
    git push git@example.org:org/repo.git HEAD:refs/heads/deploy
  '';
}
```

### Running locally with secrets

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

## State files and phases

### State files

An effect can keep small files between runs, per project:

- `getStateFile NAME [FILE]` downloads state `NAME` to `FILE` (default `NAME`).
  If the state doesn't exist yet, `FILE` is removed.
- `putStateFile NAME [FILE]` uploads `FILE`.

Call them from `getStateScript` and `putStateScript`. State is also uploaded
when the effect fails.

```nix
mkEffect {
  getStateScript = "getStateFile counter";
  effectScript = "echo $(( $(cat counter 2>/dev/null || echo 0) + 1 )) > counter";
  putStateScript = "putStateFile counter";
}
```

### Phases

Phases run in this order, each wrapped in `pre<Phase>` and `post<Phase>` hooks:

| Phase              | Script              |
| ------------------ | ------------------- |
| `initPhase`        |                     |
| `getStatePhase`    | `getStateScript`    |
| `userSetupPhase`   | `userSetupScript`   |
| `priorCheckPhase`  | `priorCheckScript`  |
| `effectPhase`      | `effectScript`      |
| `putStatePhase`    | `putStateScript`    |
| `effectCheckPhase` | `effectCheckScript` |

A failing `priorCheckScript` only prints a warning, because the effect may
repair the problem. A failing `effectCheckScript` fails the effect. Add your own
phases with `preGetStatePhases`, `preEffectPhases` and `postEffectPhases`.

```nix
mkEffect {
  inputs = [ pkgs.curl ];
  priorCheckScript = "curl -sf https://example.org/health";
  effectScript = "deploy";
  effectCheckScript = "curl -sf https://example.org/health";
}
```

## Deploying to other machines

effects-lib has hercules-ci-effects' helpers for deploying over SSH, with the
same arguments. `runNixOS` and `runNixDarwin` switch a machine to a
configuration. `ssh` runs a script on a machine.

None of them sets up an SSH key. Keep the key in a secret and write it with
`writeSSHKey` in `userSetupScript`, as the examples do.

### Switching a machine to a configuration

```nix
let
  inherit (nixbot.lib.effects { inherit pkgs; }) runNixOS runNixDarwin;
in
{
  deploy-rig = runNixOS {
    configuration = self.nixosConfigurations.rig;
    ssh.destination = "root@rig";
    secretsMap.ssh = "deploy-key";
    userSetupScript = "writeSSHKey ssh";
  };

  deploy-mac = runNixDarwin {
    configuration = self.darwinConfigurations.mac;
    ssh.destination = "admin@mac";
    secretsMap.ssh = "deploy-key";
    userSetupScript = "writeSSHKey ssh";
  };
}
```

`runNixOS` builds the configuration, copies it to the host and runs
`switch-to-configuration` there. `runNixDarwin` does the same with
`darwin-rebuild activate`, through `sudo` unless the SSH user is root or can
write the profile directory. The system that gets deployed is the effect's
`passthru.prebuilt`.

- `configuration`: an evaluated configuration, like
  `self.nixosConfigurations.rig`. A module works too. `runNixOS` evaluates it
  with `system` and `nixpkgs`, `runNixDarwin` with `nix-darwin`, `system` and
  `pkgs` (or `nixpkgs`).
- `ssh`: the connection, with the
  [options of `ssh`](#running-a-script-on-a-host). Only `destination` is
  required.
- `buildOnDestination`: shorthand for `ssh.buildOnDestination`.
- `profile` (`runNixOS` only): the profile to set. The default is
  `/nix/var/nix/profiles/system`.
- Everything else goes to `mkEffect`. `runNixDarwin` replaces `effectScript`, so
  write the key in `userSetupScript`.

### Running a script on a host

`ssh` copies the closure of a script to a host with `nix-copy-closure` and runs
the script there over `ssh`. It returns shell code for an `effectScript`:

```nix
let
  inherit (nixbot.lib.effects { inherit pkgs; }) mkEffect ssh;
in
mkEffect {
  inputs = [ pkgs.openssh ];
  secretsMap.ssh = "deploy-key";
  userSetupScript = "writeSSHKey ssh";
  effectScript = ''
    rev=v1.2.3
    ${ssh { destination = "root@rig"; inheritVariables = [ "rev" ]; } ''
      nixos-rebuild switch --flake github:org/repo/$rev
    ''}
  '';
}
```

Options:

- `destination` (required): the host, as `user@host`.
- `inheritVariables`: variables of the effect that the script can read.
- `sshOptions`, `nix-copy-closureOptions`: extra arguments for `ssh` and
  `nix-copy-closure`.
- `compress`: compress the copy and the session. `compressClosure` and
  `compressSession` set them separately.
- `useSubstitutes` (default `true`): let the host fetch paths from its
  substituters.
- `buildOnDestination`: build on the host. Needs `destinationPkgs`, a nixpkgs
  that can be built there.

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

## Tag pushes

Pushing a tag runs the tagged commit's `onPush` effects with `primaryRepo.tag`
set and `primaryRepo.branch` null, as on Hercules CI. Effects that only exist on
a tag, like the
[`github-releases`](https://flake.parts/options/hercules-ci-effects.html#opt-hercules-ci.github-releases.files)
module of hercules-ci-effects, work unchanged.

Every `onPush` effect runs on every tag, so choose in the flake which effects
belong to branches and which to tags:

```nix
herculesCI = { primaryRepo, ... }: {
  onPush.default.outputs.effects =
    if primaryRepo.tag == null then
      { deploy = mkEffect { /* ... */ }; }
    else
      lib.optionalAttrs (lib.hasPrefix "v" primaryRepo.tag) {
        release = mkEffect { /* ... */ };
      };
};
```

How tag runs behave:

- A tag waits for a branch build of the tagged tree, so
  `git push origin main v1.0` cannot leave `main` without its `onPush` run.
  Retries back off for about 15 minutes. If no branch has built the tree by
  then, the tag builds the commit itself.
- The effects run once per tag push, apart from the build's own `onPush` run.
  Tagging a commit that the default branch already deployed still releases, and
  a second tag on the same commit runs them again and keeps its own result and
  log.
- `isTag` secret conditions match, and ID tokens carry `ref: refs/tags/<tag>`.
- They show up on the build page with the event effects and are restarted the
  same way (kind `tag:<tag>`).

Limitations:

- Tag runs post no commit statuses.
- Tags are only seen through webhooks, not polled.
- An effect with `after` is skipped. Use a shared `lock` to order tag effects.
- Pushing a tag again while its run is going retries for a while, then fails.

## Event effects

`onPush` effects run when a branch is built. `onEvent` effects react to what
happens around a build: a pull request turning green, a `/command` comment, a PR
closing, a build breaking. The typical use is `tofu plan` on a pull request with
the result posted as a comment, and `tofu apply` on merge. A full example is
[examples/on-event/flake.nix](../examples/on-event/flake.nix). Hercules CI
ignores `onEvent`.

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

Definitions are **always evaluated from the default branch**, whatever pull
request the event is about. A pull request cannot change what runs by editing
`onEvent`, it only contributes data. This is unrelated to
`effects_on_pull_requests` in nixbot.toml, which is about a pull request's own
`onPush` effects.

With `checkout = true` the **PR head** is cloned to `/build/checkout`. That
clone has no push credentials and its content is untrusted, because `tofu plan`
and similar tools execute code from it. Guard such effects with
`when.permission`. Secrets and the forge token are the same as for `onPush`.

### Events

| kind                  | delivered when                                            |
| --------------------- | --------------------------------------------------------- |
| `pull_request`        | a PR head was built green, also on reopen or label change |
| `comment`             | a PR comment whose first line is `/command args`          |
| `pull_request_closed` | a PR was closed or merged                                 |
| `build_finished`      | any build finished                                        |

Bot comments are ignored, and a `/command` for an effect that is still running
gets a note instead of a second run. Deliveries are queued in the database and
retried with backoff while the forge API is unavailable. Webhooks that arrive
while nixbot itself is down are not replayed.

The event reaches the script as JSON in `$NIXBOT_EVENT_JSON` (`/run/event.json`)
and, for the common fields, as `NIXBOT_EVENT_KIND`, `NIXBOT_ACTOR`,
`NIXBOT_PR_NUMBER`, `NIXBOT_PR_HEAD`, `NIXBOT_COMMAND`, `NIXBOT_COMMAND_ARGS`,
`NIXBOT_BUILD_STATUS`, `NIXBOT_BUILD_URL`. The JSON holds up to four keys:

- `actor`: who caused it, `{ "name": "github:alice", "permission": "write" }`.
  It is absent for polled changes. Permissions come from the forge at delivery
  time.
- `pullRequest`: `number`, `title`, `url`, `author` (shaped like `actor`),
  `baseRef`, `headRef`, `headRev`, `labels`, `draft`, `isFork`, `merged`.
- `build`: `number`, `url`, `status`, `branch`, `rev`. For `build_finished` it
  also has `previousStatus` and `failedAttrs`.
- `command`, `args`: for `comment`.

### Conditions

`when` restricts an effect to some deliveries. All keys must match. An effect
that does not match is listed as skipped with the reason.

| `when` key                          | matches if                                                                                 |
| ----------------------------------- | ------------------------------------------------------------------------------------------ |
| `permission = "write"`              | actor or PR author has at least `read`/`write`/`admin` (for `comment`, only the commenter) |
| `labels = [ "deploy" ]`             | the PR has all of these labels                                                             |
| `branches = [ "main" "release-*" ]` | glob on the PR base branch, or the built branch                                            |
| `commands = [ "plan" ]`             | `comment`: the `/command` used                                                             |
| `modified = [ "terraform/*" ]`      | a file the PR changes matches a glob (pull request events only)                            |
| `status = [ "failed" ]`             | status of the build in the payload                                                         |
| `transition = "broke"` / `"fixed"`  | build status changed against the previous finished build of that branch or PR              |

### Ordering

`lock` works as for `onPush`, so an event effect never overlaps a deploy holding
the same lock. The name may contain `{pr}`: `lock = "preview-{pr}"` serialises
per pull request. A newer delivery of the same kind for the same PR (for
comments: the same command) cancels not-yet-started effects of the previous one.
`after` is not supported.

### Reporting back

Event effects post no commit statuses. `mkEffect` puts `nixbot-pr-comment` on
`PATH` instead, which comments on the pull request the event is about:
`nixbot-pr-comment "text"` or `... | nixbot-pr-comment`. With
`--replace-marker ID` it edits the comment a previous run left with that marker.
Run locally it just prints the body.

### Checks on every build

A pull request that breaks an effect should be red before it is merged, even
though its effects do not run there. Every build therefore evaluates `onPush`
and `onEvent` of the built commit and builds each effect's dependencies without
running it, like hercules-ci-agent does for a `runIf false` effect. The results
appear as "Effect checks" on the build page and count towards the `effects`
forge status. Evaluation errors are shown there too, including a broken
`onEvent` on the default branch found while delivering an event.

### Restarting

Event effects can be restarted from the build and run pages, with
`nbo build restart N --effect comment/apply`, or
`POST /api/repos/.../builds/N/effects/restart?name=apply&kind=comment`. A
running one is cancelled first. The stored payload is reused. A stuck effect can
be stopped without re-running it: `nbo build cancel N --effect NAME` or
`POST .../effects/cancel?name=...`.

### Testing locally

Describe the event with flags instead of pushing:

```console
$ nbo effects list --event pull_request --pr 7 --permission write --label preview
$ nbo effects run --event comment --command apply --args "-target=null" \
    --actor github:alice --permission admin --effect-checkout . apply
```

`list` prints every effect of that kind with the reason it would be skipped, so
a `when` can be tried without pushing. Flags cover what `when` looks at: `--pr`,
`--actor`, `--permission`, `--author-permission`, `--label`, `--modified`,
`--command`, `--args`, `--build-status`, `--previous-build-status`.
`--payload FILE` takes a payload nixbot recorded instead.
