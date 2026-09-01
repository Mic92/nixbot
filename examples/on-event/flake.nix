# `onEvent` effects: react to pull requests, PR comments, PR close and
# build results. Effect code is always taken from the default branch,
# the event only contributes data ($NIXBOT_EVENT_JSON and NIXBOT_*
# variables). See docs/EFFECTS.md "Event effects".
#
# Try locally:
#   nbo effects list --event pull_request --payload pr.json
#   nbo effects run  --event comment --payload comment.json plan
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.nixbot.url = "github:Mic92/nixbot";
  inputs.nixbot.inputs.nixpkgs.follows = "nixpkgs";

  outputs =
    { nixpkgs, nixbot, ... }:
    let
      pkgs = nixpkgs.legacyPackages.x86_64-linux;
      fx = nixbot.lib.effects { inherit pkgs; };
      inherit (fx) mkEffect;
      nbo = nixbot.packages.x86_64-linux.nixbot-cli;

      # checkout = true mounts the PR head at $NIXBOT_EFFECT_CHECKOUT (cwd).
      # That tree is untrusted: `tofu plan` executes provider code from it,
      # hence `permission = "write"` on everything below.
      tofu =
        args:
        mkEffect (
          {
            inputs = [
              pkgs.opentofu
              nbo
            ];
            checkout = true;
            userSetupScript = "cd infra && tofu init -input=false";
          }
          // args
        );
      planScript = ''
        tofu plan -input=false -no-color -lock=false | tee "$TMPDIR/plan.txt"
        nbo pr comment --replace-marker tofu-plan --file "$TMPDIR/plan.txt"
      '';
    in
    {
      herculesCI =
        { ... }:
        {
          # hercules-compatible part: main applies for real.
          onPush.default.outputs.effects.apply = tofu {
            lock = "infra";
            effectScript = "tofu apply -input=false -auto-approve";
          };

          onEvent = {
            # A PR build turned green.
            pull_request = {
              plan = tofu {
                # actor (pusher) or PR author needs write access
                when.permission = "write";
                # queues behind a running apply
                lock = "infra";
                effectScript = planScript;
              };
              preview = mkEffect {
                when = {
                  permission = "write";
                  labels = [ "preview" ];
                };
                lock = "preview-{pr}";
                inputs = [ nbo ];
                effectScript = ''
                  echo deploy "pr-$NIXBOT_PR_NUMBER" at "$NIXBOT_PR_HEAD"
                  nbo pr comment --replace-marker preview "https://pr-$NIXBOT_PR_NUMBER.preview.example.org"
                '';
              };
            };

            # PR closed or merged. Same lock as `preview`, so a deploy for
            # the last push finishes before teardown starts.
            pull_request_closed.teardown = mkEffect {
              lock = "preview-{pr}";
              effectScript = ''echo destroy "pr-$NIXBOT_PR_NUMBER"'';
            };

            # "/plan" and "/apply" comments on open PRs.
            comment = {
              plan = tofu {
                when = {
                  commands = [ "plan" ];
                  permission = "write";
                };
                lock = "infra";
                effectScript = planScript;
              };
              apply = tofu {
                when = {
                  commands = [ "apply" ];
                  permission = "write";
                  # refuse a red or unbuilt head
                  status = [ "succeeded" ];
                };
                lock = "infra";
                effectScript = ''
                  tofu apply -input=false -auto-approve
                  nbo pr comment "Applied $NIXBOT_PR_HEAD (requested by $NIXBOT_ACTOR)."
                '';
              };
            };

            # Edge-triggered notifications for main.
            build_finished = {
              broke = mkEffect {
                when = {
                  branches = [ "main" ];
                  transition = "broke";
                };
                effectScript = ''
                  echo "main broke: $NIXBOT_BUILD_URL"
                  jq -r '.build.failedAttrs[]' "$NIXBOT_EVENT_JSON"
                '';
              };
              fixed = mkEffect {
                when = {
                  branches = [ "main" ];
                  transition = "fixed";
                };
                effectScript = ''echo "main is green again: $NIXBOT_BUILD_URL"'';
              };
            };
          };
        };
    };
}
