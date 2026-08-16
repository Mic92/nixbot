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
    # Fire-and-forget: the target enqueues a detached flakelet update,
    # which restarts nixbot and with it this effect runner.
    # flakelet until the branch merges to main
    effects.deploy =
      hci-effects.runIf
        (builtins.elem (primaryRepo.branch or null) [
          "main"
          "flakelet"
        ])
        (
          hci-effects.mkEffect {
            name = "deploy";
            idTokenAudiences = [ "step-ca-ssh" ];
            inputs = [
              pkgs.step-cli
              pkgs.openssh
            ];
            effectScript = ''
              export STEPPATH=$PWD/.step
              step ca bootstrap --ca-url https://ca.r \
                --fingerprint 759759ea7dc7d635d761ce19a07bc0b3ab02212318e05b49d2b194c60414b84a

              ssh-keygen -t ed25519 -N "" -q -f ./id_deploy
              step ssh certificate --sign --provisioner nixbot \
                --token "$(nixbot-id-token step-ca-ssh)" \
                deploy ./id_deploy.pub

              ssh -i ./id_deploy \
                -o CertificateFile=./id_deploy-cert.pub \
                -o UserKnownHostsFile=$PWD/known_hosts \
                -o StrictHostKeyChecking=accept-new \
                nixbot-deploy@eve.r deploy
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
