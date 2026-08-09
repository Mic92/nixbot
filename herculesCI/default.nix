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
    effects.deploy = hci-effects.runIf (primaryRepo.branch or null == "main") (
      hci-effects.mkEffect {
        effectScript = ''
          echo "${builtins.toJSON { inherit (primaryRepo) branch tag rev; }}"
          ${pkgs.hello}/bin/hello
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
