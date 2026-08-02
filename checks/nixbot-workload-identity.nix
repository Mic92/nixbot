# End-to-end workload identity, mirroring the docs/WORKLOAD_IDENTITY.md
# example: an effect requests an ID token from nixbot, logs into OpenBao
# (which verifies it against nixbot's JWKS), trades it for a short-lived
# SSH certificate from OpenBao's SSH CA and logs into the host with it.
let
  audience = "https://openbao.example";
  openbaoAddr = "http://127.0.0.1:8200";
  deployScript =
    pkgs:
    pkgs.writeShellScript "vault-ssh-deploy" ''
      set -euo pipefail
      export PATH=${pkgs.curl}/bin:${pkgs.jq}/bin:${pkgs.openbao}/bin:${pkgs.openssh}/bin:$PATH
      export BAO_ADDR=${openbaoAddr}
      # The sandbox has an empty /etc; ssh needs a passwd entry for its uid.
      echo "root:x:0:0:root:$HOME:/bin/sh" > /etc/passwd

      # Authenticate to OpenBao with the nixbot-issued identity token.
      jwt=$(curl -fsS -X POST "$NIXBOT_ID_TOKEN_REQUEST_URL" \
        -H "Authorization: Bearer $NIXBOT_ID_TOKEN_REQUEST_TOKEN" \
        -H "Content-Type: application/json" \
        --data '{"audience": "${audience}"}' | jq -re .token)
      BAO_TOKEN=$(bao write -field=token auth/jwt/login role=deploy jwt="$jwt")
      export BAO_TOKEN

      # Trade it for a short-lived SSH certificate and deploy with it:
      # no deploy key is stored in CI.
      ssh-keygen -t ed25519 -N "" -f ./id_deploy
      bao write -field=signed_key ssh/sign/deploy \
        public_key=@./id_deploy.pub valid_principals=deploy > ./id_deploy-cert.pub
      ssh -i ./id_deploy -o CertificateFile=./id_deploy-cert.pub \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        deploy@127.0.0.1 'echo deployed > /home/deploy/deployed'
    '';
  flakeText = pkgs: ''
    {
      outputs = { self }: {
        checks.${pkgs.stdenv.hostPlatform.system}.test = derivation {
          name = "test";
          system = "${pkgs.stdenv.hostPlatform.system}";
          builder = "/bin/sh";
          args = [ "-c" "echo hello > $out" ];
        };
        herculesCI.onPush.default.outputs.effects.deploy = derivation {
          name = "deploy";
          system = "${pkgs.stdenv.hostPlatform.system}";
          # The effect sandbox uses `nix develop`, which requires bash as
          # the builder and an `outputs` env var for its env dump.
          builder = "${pkgs.runtimeShell}";
          args = [ "${deployScript pkgs}" ];
          outputs = [ "out" ];
          isEffect = true;
          idTokenAudiences = builtins.toJSON [ "${audience}" ];
        };
      };
    }
  '';
in
(import ./lib.nix) {
  name = "nixbot-workload-identity";
  nodes.machine =
    { pkgs, ... }:
    {
      imports = [ (import ./github-node.nix { inherit flakeText; }) ];

      # The raw test effect references bash and the login script without
      # declaring them as derivation inputs, so they are only visible to
      # the builder without the nix build sandbox (the host store is
      # mounted into the VM).
      nix.settings.sandbox = false;

      environment.systemPackages = [ pkgs.openbao ];

      # Deploy target: the effect logs in as "deploy" with a certificate
      # signed by OpenBao's SSH CA. sshd reads the CA key per
      # authentication, so the test script can install it after the SSH
      # engine is configured.
      services.openssh = {
        enable = true;
        extraConfig = "TrustedUserCAKeys /etc/ssh/vault-ca.pub";
      };
      users.users.deploy.isNormalUser = true;

      systemd.services.openbao-dev = {
        wantedBy = [ "multi-user.target" ];
        # Dev mode shells out to `sh` and needs a writable HOME when
        # expanding its config paths.
        path = [ pkgs.bash ];
        environment.HOME = "/var/lib/openbao-dev";
        serviceConfig = {
          ExecStart = "${pkgs.openbao}/bin/bao server -dev -dev-root-token-id=root -dev-listen-address=127.0.0.1:8200";
          StateDirectory = "openbao-dev";
        };
      };
    };

  testScript = ''
    ${builtins.readFile ./github-helpers.py}

    machine.start()
    machine.wait_for_unit("nixbot.service")
    machine.wait_until_succeeds(
        "curl --fail -s http://127.0.0.1:8010/health", timeout=120
    )

    with subtest("nixbot serves OIDC discovery and JWKS"):
        doc = json.loads(machine.succeed(
            "curl --fail -s http://127.0.0.1:8010/.well-known/openid-configuration"
        ))
        assert doc["issuer"] == "http://localhost:8010", doc
        jwks = json.loads(machine.succeed(
            "curl --fail -s http://127.0.0.1:8010/.well-known/jwks.json"
        ))
        assert jwks["keys"], jwks

    with subtest("openbao trusts nixbot as JWT auth backend"):
        machine.wait_for_open_port(8200)
        bao = "BAO_ADDR=${openbaoAddr} BAO_TOKEN=root bao "
        machine.succeed(bao + "auth enable jwt")
        machine.succeed(
            bao + "write auth/jwt/config oidc_discovery_url=http://localhost:8010"
        )
        machine.succeed(
            "echo 'path \"ssh/sign/deploy\" { capabilities = [\"update\"] }' | "
            + bao + "policy write ssh-deploy -"
        )
        machine.succeed(
            bao + "write auth/jwt/role/deploy role_type=jwt user_claim=sub "
            "bound_audiences=${audience} "
            "bound_subject=repo:github:acme/test-flake:ref:refs/heads/master "
            "policies=ssh-deploy token_ttl=5m"
        )

    with subtest("openbao's SSH CA can sign certificates for the deploy user"):
        machine.succeed(bao + "secrets enable ssh")
        machine.succeed(bao + "write ssh/config/ca generate_signing_key=true")
        machine.succeed(
            bao + "write ssh/roles/deploy key_type=ca allow_user_certificates=true "
            "allowed_users=deploy default_user=deploy ttl=10m"
        )
        machine.succeed(
            bao + "read -field=public_key ssh/config/ca > /etc/ssh/vault-ca.pub"
        )

    with subtest("push webhook triggers build and the SSH deploy effect"):
        push_webhook(machine)

        def effect_succeeded(_ignore):
            done = completed_check_runs(machine)
            effects = {
                name: conclusion
                for name, conclusion in done.items()
                if "default.deploy" in name
            }
            return effects and all(c == "success" for c in effects.values())

        retry(effect_succeeded, timeout_seconds=600)
        machine.succeed("test -e /home/deploy/deployed")
  '';
}
