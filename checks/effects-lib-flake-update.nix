# effects-lib's `flakeUpdate`: its setup and effect scripts run in a
# checkout set up as nixbot sets one up, with the checkout's token in
# origin and in an insteadOf for the forge. The forge is local bare
# repositories behind an http URL (a global insteadOf that only applies
# once the effect has dropped the checkout's) and a fake API at it. The
# stand-in nix rewrites flake.lock and commits it like
# `nix flake update --commit-lock-file`.
{ pkgs, ... }:
let
  inherit (pkgs) lib;
  effects = import ../herculesCI/effects-lib.nix { inherit pkgs; };
  fakeNix = pkgs.writeShellScriptBin "nix" ''
    printf '%s\n' "$*" >>"$TMPDIR/nix-args"
    echo "{ \"run\": \"$RANDOM$RANDOM\" }" >flake.lock
    git commit -q -m "flake.lock: Update" -m "Updated input 'nixpkgs'" flake.lock
  '';
  fakeForge = pkgs.writers.writePython3 "fake-forge" { } ''
    import http.server
    import json
    import re
    import sys

    # The forges' replies to a second pull request for the same branch.
    EXISTS = {
        "/api/v3/repos/acme/{}/pulls": (422, {
            "message": "Validation Failed",
            "errors": [{"message": "A pull request already exists for "
                                   "acme:flake-update."}],
        }),
        "/api/v1/repos/acme/{}/pulls": (409, {
            "message": "pull request already exists for these targets",
        }),
        "/api/v4/projects/acme%2F{}/merge_requests": (409, {
            "message": ["Another open merge request already exists for "
                        "this source branch: !1"],
        }),
    }
    seen = set()


    class Forge(http.server.BaseHTTPRequestHandler):
        def reply(self, status, body):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def record(self):
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            with open(sys.argv[2], "a") as log:
                entry = {"method": self.command, "path": self.path,
                         "body": body, "auth": self.headers["Authorization"]}
                print(json.dumps(entry, sort_keys=True, separators=(",", ":")),
                      file=log)
            return body

        def do_POST(self):
            body = self.record()
            if self.path == "/api/graphql":
                # The "clean" repository's pull request is mergeable now.
                if body["variables"]["id"] == "PR_clean":
                    return self.reply(200, {"errors": [{
                        "message": "Pull request is in clean status"}]})
                return self.reply(200, {"data": {}})
            repo = re.search(r"acme(?:/|%2F)([^/]+)", self.path)[1]
            template = self.path.replace(repo, "{}", 1)
            if self.path in seen:
                return self.reply(*EXISTS[template])
            seen.add(self.path)
            self.reply(201, {"number": 1, "node_id": f"PR_{repo}"})

        def do_PUT(self):
            self.record()
            self.reply(200, {"merged": True})

        def log_message(self, *args):
            pass


    server = http.server.HTTPServer(("127.0.0.1", 0), Forge)
    with open(sys.argv[1], "w") as f:
        f.write(str(server.server_port))
    server.serve_forever()
  '';
  effectFor =
    forgeType: args:
    effects.flakeUpdate (
      {
        inherit forgeType;
        nix = fakeNix;
        baseMergeMethod = "reset";
        pullRequestTitle = "chore: update flake.lock";
      }
      // args
    );
  hook =
    lib.findFirst (p: lib.getName p == "hercules-ci-effect-sh") (throw "no setup hook")
      (effects.mkEffect { }).nativeBuildInputs;
  run =
    {
      forge,
      repo ? "widget",
      effect,
      # Without a baseMergeBranch the update branch is updated as it stands.
      resets ? true,
      expected,
    }:
    ''
      remote=$TMPDIR/forge/acme/${repo}.git
      rm -rf "$TMPDIR/forge" "$TMPDIR/checkout" "$TMPDIR/requests" "$HOME"
      mkdir -p "$HOME"
      git init -q --bare -b develop "$remote"
      git clone -q "$remote" "$TMPDIR/seed" 2>/dev/null
      (cd "$TMPDIR/seed" && echo '{}' >flake.lock && git add flake.lock \
        && git commit -qm init && git push -q origin HEAD:develop)
      rm -rf "$TMPDIR/seed"
      develop=$(git -C "$remote" rev-parse develop)

      # The forge's git side. git takes the longest matching insteadOf, so
      # while the checkout's (one character longer) is there, URLs go to
      # the fake API with its token and fail.
      forge="http://127.0.0.1:$port"
      git config --global url."$TMPDIR/forge".insteadOf "$forge"
      # nixbot's checkout: its token in origin and in every forge URL.
      git clone -q "$remote" "$TMPDIR/checkout"
      git -C "$TMPDIR/checkout" remote set-url origin \
        "http://x-access-token:checkout-token@127.0.0.1:$port/acme/${repo}.git"
      git -C "$TMPDIR/checkout" config \
        url."http://x-access-token:checkout-token@127.0.0.1:$port/".insteadOf "$forge/"

      for attempt in 1 2; do
        (
          cd "$TMPDIR/checkout"
          export PATH=${lib.makeBinPath effect.nativeBuildInputs}:$PATH
          export NIXBOT_EFFECT_CHECKOUT=$TMPDIR/checkout
          set -euo pipefail
          eval ${lib.escapeShellArg effect.userSetupScript}
          eval ${lib.escapeShellArg effect.effectScript}
        )
      done

      # Fetched and pushed with the effect's token, not the checkout's.
      grep -qx "http://${
        if forge == "gitlab" then "oauth2" else "x-access-token"
      }:secret-token@127.0.0.1:$port" ~/.git-credentials
      ${lib.optionalString (forge == "github") ''
        grep -qx "access-tokens = 127.0.0.1:$port=secret-token" ~/.config/nix/nix.conf
      ''}
      [[ $(grep -c . "$TMPDIR/nix-args") == 2 ]] && rm "$TMPDIR/nix-args"
      ${
        if resets then
          ''
            # Reset: each run starts again from develop and force-pushes.
            [[ $(git -C "$remote" rev-parse flake-update^) == "$develop" ]]
          ''
        else
          ''
            # No base merge: the second run updates the branch as it stands.
            [[ $(git -C "$remote" rev-parse flake-update~2) == "$develop" ]]
          ''
      }
      diff "$TMPDIR/requests" ${pkgs.writeText "${forge}-${repo}-requests" expected}
    '';
  create =
    forge: repo:
    {
      github = {
        method = "POST";
        auth = "Bearer secret-token";
        body = {
          base = "develop";
          body = "Updated input 'nixpkgs'";
          head = "flake-update";
          title = "chore: update flake.lock";
        };
        path = "/api/v3/repos/acme/${repo}/pulls";
      };
      gitea = {
        method = "POST";
        auth = "token secret-token";
        body = {
          base = "develop";
          body = "Updated input 'nixpkgs'";
          head = "flake-update";
          title = "chore: update flake.lock";
        };
        path = "/api/v1/repos/acme/${repo}/pulls";
      };
      gitlab = {
        method = "POST";
        auth = "Bearer secret-token";
        body = {
          description = "Updated input 'nixpkgs'";
          source_branch = "flake-update";
          target_branch = "develop";
          title = "chore: update flake.lock";
        };
        path = "/api/v4/projects/acme%2F${repo}/merge_requests";
      };
    }
    .${forge};
  autoMerge = repo: method: {
    method = "POST";
    auth = "Bearer secret-token";
    body = {
      query = "mutation ($id: ID!, $method: PullRequestMergeMethod!) { enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) { clientMutationId } }";
      variables = {
        id = "PR_${repo}";
        method = lib.toUpper method;
      };
    };
    path = "/api/graphql";
  };
  # One create per run; the second finds the pull request open, so only
  # the first enables auto-merge.
  lines = map builtins.toJSON;
  requests = entries: lib.concatMapStrings (e: e + "\n") (lines entries);
  cases = [
    {
      forge = "github";
      effect = effectFor "github" { baseMergeBranch = "develop"; };
      expected = requests [
        (create "github" "widget")
        (create "github" "widget")
      ];
    }
    {
      forge = "gitea";
      effect = effectFor "gitea" { baseMergeBranch = "develop"; };
      expected = requests [
        (create "gitea" "widget")
        (create "gitea" "widget")
      ];
    }
    {
      # GitLab leaves the base to the repository's default branch.
      forge = "gitlab";
      effect = effectFor "gitlab" { };
      resets = false;
      expected = requests [
        (create "gitlab" "widget")
        (create "gitlab" "widget")
      ];
    }
    {
      forge = "github";
      repo = "automerge";
      effect = effectFor "github" {
        baseMergeBranch = "develop";
        autoMergeMethod = "squash";
      };
      expected = requests [
        (create "github" "automerge")
        (autoMerge "automerge" "squash")
        (create "github" "automerge")
      ];
    }
    {
      # Already mergeable: merged at once, as upstream does.
      forge = "github";
      repo = "clean";
      effect = effectFor "github" {
        baseMergeBranch = "develop";
        autoMergeMethod = "rebase";
      };
      expected = requests [
        (create "github" "clean")
        (autoMerge "clean" "rebase")
        {
          method = "PUT";
          auth = "Bearer secret-token";
          body.merge_method = "rebase";
          path = "/api/v3/repos/acme/clean/pulls/1/merge";
        }
        (create "github" "clean")
      ];
    }
  ];
in
pkgs.runCommand "effects-lib-flake-update"
  {
    nativeBuildInputs = [
      pkgs.git
      pkgs.jq
    ];
  }
  ''
    export HOME=$TMPDIR/home IN_HERCULES_CI_EFFECT=true
    # Who seeds the test repositories. On Linux builders git cannot guess.
    export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com
    export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com
    export HERCULES_CI_SECRETS_JSON=$TMPDIR/secrets.json
    echo '{ "token": { "data": { "token": "secret-token" } } }' >"$HERCULES_CI_SECRETS_JSON"
    source ${hook}/nix-support/setup-hook
    ${fakeForge} "$TMPDIR/port" "$TMPDIR/requests" &
    # A failing case must not leave the build waiting on the forge.
    trap 'kill %1' EXIT
    while [[ ! -s $TMPDIR/port ]]; do sleep 0.1; done
    port=$(cat "$TMPDIR/port")

    ${lib.concatMapStrings run cases}
    touch $out
  ''
