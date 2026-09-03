{
  self,
  pkgs,
  ...
}:
let
  inherit (pkgs) lib;
  hci-effects = import ./effects-lib.nix { inherit pkgs; };
  system = pkgs.stdenv.hostPlatform.system;
  docs = self.packages.${system}.docs;
in
{ primaryRepo, ... }:
{
  onPush.default.outputs = {
    # The update restarts nixbot and with it this effect runner, so do
    # not wait for the result. Authorized by rule "nixbot" in
    # Mic92/dotfiles nixosModules/flakelet-relay.
    effects.deploy = hci-effects.runIf (primaryRepo.branch or null == "main") (
      hci-effects.mkEffect {
        name = "deploy";
        idTokenAudiences = [ "flakelet-relay" ];
        inputs = [ (pkgs.callPackage ./flakelet-push.nix { }) ];
        effectScript = ''
          export FLAKELET_RELAY_TOKEN_COMMAND="nixbot-id-token flakelet-relay"
          flakelet-push --relay-srv thalheim.io deploy --detach eve/nixbot
        '';
      }
    );
    # Dogfood effects: publish the docs site to the gh-pages branch.
    effects.gh-pages = hci-effects.runIf (primaryRepo.branch or null == "main") (
      hci-effects.mkEffect {
        name = "gh-pages";
        inputs = [
          pkgs.git
          pkgs.openssh
        ];
        secretsMap.github.type = "GitToken";
        effectScript = ''
          token=$(jq -r '.github.data.token' "$HERCULES_CI_SECRETS_JSON")
          remote=$(printf '%s' ${lib.escapeShellArg primaryRepo.remoteHttpUrl} \
            | sed "s#https://#https://x-access-token:$token@#")

          git config --global user.email "nixbot@nix-community.org"
          git config --global user.name "nixbot"

          work=$(mktemp -d)
          cp -r --no-preserve=mode,ownership ${docs}/. "$work/"
          touch "$work/.nojekyll"

          cd "$work"
          git init -q -b gh-pages
          git add -A
          git commit -q -m "Deploy docs for ${primaryRepo.rev}"
          git push -f "$remote" gh-pages
        '';
      }
    );
  };
}
