# Real GitLab is heavyweight (multi-GB closure, minutes of startup),
# hence a check separate from nixbot.nix. Flow under test:
# PAT-based discovery, webhook auto-registration, push-triggered
# eval/build, commit statuses posted back.
#
# Deliberately a qemu VM, not an nspawn container: nixbot runs nix
# builds, and test containers share the host store read-only without a
# nix database.
(import ./lib.nix) {
  name = "nixbot-gitlab";
  nodes.gitlab =
    {
      self,
      pkgs,
      lib,
      ...
    }:
    {
      imports = [ self.nixosModules.nixbot ];

      # Restart loops only mask startup failures in a test.
      systemd.services.gitlab.serviceConfig.Restart = lib.mkForce "no";
      systemd.services.gitlab-workhorse.serviceConfig.Restart = lib.mkForce "no";
      systemd.services.gitaly.serviceConfig.Restart = lib.mkForce "no";
      systemd.services.gitlab-sidekiq.serviceConfig.Restart = lib.mkForce "no";

      services.gitlab = {
        enable = true;
        host = "gitlab";
        # Advertised in clone URLs (http_url_to_repo); must match the
        # nginx vhost, not the internal puma port (default 8080).
        port = 80;
        initialRootPasswordFile = pkgs.writeText "root-password" "notproduction";
        secrets = {
          secretFile = pkgs.writeText "secret" "Aig5zaic";
          otpFile = pkgs.writeText "otpsecret" "Riew9mue";
          dbFile = pkgs.writeText "dbsecret" "we2quaeZ";
          jwsFile = pkgs.runCommand "oidcKeyBase" { } "${pkgs.openssl}/bin/openssl genrsa 2048 > $out";
          activeRecordPrimaryKeyFile = pkgs.writeText "arprimary" "vsaYPZjmJ3a14gqLUnOQ";
          activeRecordDeterministicKeyFile = pkgs.writeText "ardeterministic" "DOROVzNNM9PXebBROBnL";
          activeRecordSaltFile = pkgs.writeText "arsalt" "QlPCMjGLtRYXf1ssAGav";
        };
        sidekiq.concurrency = 1;
      };

      services.nginx = {
        enable = true;
        recommendedProxySettings = true;
        virtualHosts.gitlab.locations."/".proxyPass = "http://unix:/run/gitlab/gitlab-workhorse.socket";
      };
      # IPv4-only aliases: "localhost" resolves to ::1 first, but the
      # nixbot listens on 0.0.0.0, so GitLab's webhook delivery to
      # localhost would die on connection refused.
      networking.hosts."127.0.0.1" = [
        "gitlab"
        "buildbot"
      ];

      services.nixbot = {
        enable = true;
        domain = "localhost";
        nginx.enable = false;
        # Port 80 is the GitLab vhost; deliver webhooks straight to the
        # service under its IPv4 alias.
        webhookBaseUrl = "http://buildbot:8010";
        gitlab = {
          enable = true;
          instanceUrl = "http://gitlab";
          tokenFile = "/var/lib/secrets/gitlab-token";
        };
      };
      # The token is minted at runtime; don't fail before it exists.
      systemd.services.nixbot = {
        after = [ "setup-gitlab.service" ];
        requires = [ "setup-gitlab.service" ];
      };

      nix.settings.experimental-features = [
        "nix-command"
        "flakes"
      ];

      environment.systemPackages = [
        pkgs.git
        pkgs.curl
        pkgs.jq
      ];

      systemd.services.setup-gitlab = {
        wantedBy = [ "multi-user.target" ];
        after = [ "gitlab.service" ];
        path = [
          pkgs.curl
          pkgs.jq
          pkgs.git
          pkgs.coreutils
          pkgs.bash
          pkgs.util-linux
          pkgs.systemd
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          TimeoutStartSec = "20min";
          Environment = "HOME=/root";
        };
        script = ''
          set -x
          timeout 900 bash -c 'until curl -fs http://gitlab/users/sign_in > /dev/null; do sleep 5; done'

          TOKEN=nixbot-test-token-of-20-chars
          runuser -u gitlab -- /run/current-system/sw/bin/gitlab-rails runner "
            ApplicationSetting.current.update!(allow_local_requests_from_web_hooks_and_services: true)
            Rails.cache.clear
            token = User.find_by_username('root').personal_access_tokens.create!(
              name: 'nixbot', scopes: [:api], expires_at: 7.days.from_now)
            token.set_token('$TOKEN')
            token.save!
          "
          systemctl restart gitlab-sidekiq.service

          mkdir -p /var/lib/secrets
          echo "$TOKEN" > /var/lib/secrets/gitlab-token
          chmod 644 /var/lib/secrets/gitlab-token

          # The legacy-import topic enables the project on first discovery.
          # gitaly may not be ready yet even though the web UI answers.
          timeout 300 bash -c 'until curl -fs -H "PRIVATE-TOKEN: $0" http://gitlab/api/v4/projects/root%2Ftest-flake > /dev/null; do
            curl -s -X POST -H "PRIVATE-TOKEN: $0" -H "Content-Type: application/json" \
              -d "{\"name\":\"test-flake\",\"visibility\":\"public\",\"topics\":[\"build-with-buildbot\"]}" \
              http://gitlab/api/v4/projects | jq -c "{id, message, error}"
            sleep 5
          done' "$TOKEN"

          mkdir -p /tmp/test-flake
          cd /tmp/test-flake
          git init -b master
          git config user.name test
          git config user.email test@example.com
          cat > flake.nix <<'EOF'
          {
            outputs = { self }: {
              checks.x86_64-linux.test = derivation {
                name = "test";
                system = "x86_64-linux";
                builder = "/bin/sh";
                args = [ "-c" "echo hello > $out" ];
              };
            };
          }
          EOF
          git add flake.nix
          git commit -m "initial commit"
          git remote add origin "http://oauth2:$TOKEN@gitlab/root/test-flake.git"
          git push -u origin master
        '';
      };

      virtualisation.memorySize = 6144;
      virtualisation.cores = 4;
      virtualisation.useNixStoreImage = true;
      virtualisation.writableStore = true; # nixbot builds into the store
    };

  testScript = ''
    import json

    start_all()

    gitlab.wait_for_unit("setup-gitlab.service", timeout=1800)
    gitlab.wait_for_unit("nixbot.service")
    gitlab.wait_until_succeeds(
        "curl --fail -s http://127.0.0.1:8010/health", timeout=120
    )

    with subtest("project discovered and webhook registered"):
        def hook_registered(_ignore):
            out = gitlab.succeed(
                "TOKEN=$(cat /var/lib/secrets/gitlab-token); "
                'curl -fs -H "PRIVATE-TOKEN: $TOKEN" '
                "http://gitlab/api/v4/projects/root%2Ftest-flake/hooks"
            )
            hooks = json.loads(out)
            print(hooks)
            return any(
                h["url"].endswith("/webhooks/gitlab")
                and h["push_events"]
                and h["merge_requests_events"]
                for h in hooks
            )

        retry(hook_registered, timeout_seconds=300)

    with subtest("push delivers the webhook"):
        # Hooks are delivered by sidekiq, which setup-gitlab restarted and
        # which needs minutes to boot Rails again on a loaded builder.
        gitlab.wait_until_succeeds(
            "journalctl -u gitlab-sidekiq _SYSTEMD_INVOCATION_ID=$(systemctl show -P InvocationID gitlab-sidekiq) | grep -q 'Booted Rails'",
            timeout=600,
        )
        gitlab.succeed(
            "cd /tmp/test-flake && "
            "echo '# trigger' >> flake.nix && "
            "git add flake.nix && git commit -m 'trigger build' && "
            "git push origin master"
        )
        sha = gitlab.succeed("git -C /tmp/test-flake rev-parse master").strip()

        # Early cutoff: a build for the pushed commit must exist before
        # waiting minutes on statuses, otherwise delivery is broken.
        def build_created(_ignore):
            out = gitlab.succeed(
                "curl --fail -s "
                f"'http://127.0.0.1:8010/api/repos/gitlab/root/test-flake/builds?commit={sha}'"
            )
            builds = json.loads(out)["items"]
            print(builds)
            return bool(builds)

        try:
            retry(build_created, timeout_seconds=120)
        except Exception:
            print(gitlab.execute("journalctl -u nixbot --no-pager | tail -50")[1])
            print(gitlab.execute(
                "runuser -u gitlab -- /run/current-system/sw/bin/gitlab-rails runner "
                "'puts WebHookLog.last(5).map { |l| "
                "[l.url, l.response_status, l.internal_error_message, l.response_body].inspect }'"
            )[1])
            raise

    with subtest("eval and build post commit statuses"):

        def statuses_posted(_ignore):
            out = gitlab.succeed(
                "TOKEN=$(cat /var/lib/secrets/gitlab-token); "
                'curl -fs -H "PRIVATE-TOKEN: $TOKEN" '
                f"http://gitlab/api/v4/projects/root%2Ftest-flake/repository/commits/{sha}/statuses"
            )
            statuses = json.loads(out)
            print(statuses)
            done = {
                s["name"]: s["status"]
                for s in statuses
                if s["status"] in ("success", "failed")
            }
            return (
                done.get("nixbot/nix-eval") == "success"
                and done.get("nixbot/nix-build") == "success"
            )

        retry(statuses_posted, timeout_seconds=600)
  '';
}
