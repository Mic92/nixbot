# Tangled Integration

[Tangled](https://tangled.org) has no API to post commit statuses to. CI results
come from a _spindle_ that the repository names and that the Tangled web app
queries. nixbot does not implement a spindle itself. It is driven by
[tack](https://github.com/mitchellh/tack), a spindle that hands Tangled
pipelines to an existing CI system, and serves the part of the Buildkite REST
API that tack uses:

```
Tangled knot ──trigger──▶ tack ──POST …/builds──▶ nixbot
Tangled UI  ◀──status──── tack ◀──webhook──────── nixbot
                           └────GET jobs, logs──▶ nixbot
```

## Requirements

- A running tack that is registered as a spindle on Tangled, see the tack
  documentation.
- tack must support `TACK_BUILDKITE_API_URL`.
- The repositories must be fetchable by nixbot without credentials.

## Step 1: Configure nixbot

```nix
services.nixbot = {
  tangled = {
    enable = true;
    # Shared with tack as TACK_BUILDKITE_TOKEN.
    apiTokenFile = "/path/to/tangled-api-token";
    webhookUrl = "https://tack.example.org/webhooks/buildkite";
    # Shared with tack as TACK_BUILDKITE_WEBHOOK_SECRET.
    webhookSecretFile = "/path/to/tangled-webhook-secret";

    # Keyed by the pipeline slug the workflow names.
    repositories.widget = {
      url = "https://knot.example.org/alice.example.org/widget";
      # defaultBranch = "main";
    };
  };
};
```

Both secrets are arbitrary high-entropy strings, e.g. `openssl rand -hex 32`.

Listing a repository enables it. There is no discovery: tack only tells nixbot a
pipeline slug, so nixbot has to know the clone URL in advance.

## Step 2: Configure tack

| Env var                         | Value                                  |
| ------------------------------- | -------------------------------------- |
| `TACK_BUILDKITE_API_URL`        | `https://nixbot.example.org/buildkite` |
| `TACK_BUILDKITE_TOKEN`          | content of `apiTokenFile`              |
| `TACK_BUILDKITE_ORG`            | `nixbot` (`tangled.organization`)      |
| `TACK_BUILDKITE_WEBHOOK_SECRET` | content of `webhookSecretFile`         |
| `TACK_BUILDKITE_WEBHOOK_MODE`   | `signature`                            |

## Step 3: Add a workflow to the repository

Select the spindle in the repository settings on Tangled, then commit
`.tangled/workflows/nixbot.yml`:

```yaml
engine: tack

when:
  - event: ["push", "pull_request"]
    branch: ["main"]

tack:
  buildkite:
    pipeline: widget
```

`when:` decides what is built. nixbot's own branch filter does not apply to
Tangled repositories.

## What Tangled shows

A pipeline has one step per evaluated attribute that was built (cached
attributes are left out), one per effect, and a `nix-eval` step with the
summary. Each step carries its build log. The pipeline finishes when the build
and its effects have finished.

Retrying a pipeline in Tangled restarts the failed attributes of the nixbot
build.

## Limitations

- Pull requests are built as the merge of the source branch into the target
  branch. Tack does not pass the pull request's number on, so nixbot counts pull
  requests per repository itself and the number in the nixbot UI differs from
  the one on Tangled.
- Pull requests from forks are not supported: tack only names the source branch,
  which nixbot looks for in the configured repository.
- There is no approval gate. Whoever may trigger the spindle may run a build.
- Cancelling a pipeline in Tangled does not cancel the nixbot build, tack has no
  such call for Buildkite.
