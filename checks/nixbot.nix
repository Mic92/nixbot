let
  # Both nodes build the same minimal flake with one check.
  testFlake = ''
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
  '';
  setupTestFlake = ''
    mkdir -p /tmp/test-flake
    cd /tmp/test-flake
    git init -b master
    git config user.name test
    git config user.email test@example.com
    cat > flake.nix <<'EOF'
    ${testFlake}
    EOF
    git add flake.nix
    git commit -m "initial commit"
  '';
in
(import ./lib.nix) {
  name = "nixbot";
  nodes = {
    # GitHub mode against a fake GitHub API: discovery, webhook, eval,
    # build, and commit-status assertions all run against local fakes.
    github = import ./github-node.nix { flakeText = _pkgs: testFlake; };

    # Gitea mode against a real Gitea: discovery registers the webhook,
    # a push delivers it, and nixbot posts commit statuses back.
    gitea =
      { self, pkgs, ... }:
      {
        imports = [ self.nixosModules.nixbot ];

        services.gitea = {
          enable = true;
          settings = {
            server = {
              HTTP_PORT = 3742;
              ROOT_URL = "http://localhost:3742/";
              DOMAIN = "localhost";
            };
            security.INSTALL_LOCK = true;
            webhook.ALLOWED_HOST_LIST = "localhost";
          };
          database.type = "sqlite3";
        };

        services.nixbot = {
          enable = true;
          domain = "localhost";
          gitea = {
            enable = true;
            instanceUrl = "http://localhost:3742";
            tokenFile = "/var/lib/secrets/gitea-token";
            topic = "nixbot";
          };
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

        networking.firewall.enable = false;

        systemd.services.setup-gitea = {
          wantedBy = [ "multi-user.target" ];
          after = [ "gitea.service" ];
          requires = [ "gitea.service" ];
          before = [ "nixbot.service" ];
          requiredBy = [ "nixbot.service" ];
          path = [
            pkgs.gitea
            pkgs.git
            pkgs.curl
            pkgs.jq
            pkgs.coreutils
            pkgs.util-linux
            pkgs.bash
          ];
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            Environment = "HOME=/root";
          };
          script = ''
            set -x
            timeout 60 bash -c 'until curl -fs http://localhost:3742/api/v1/version; do sleep 1; done'

            cd /var/lib/gitea
            runuser -u gitea -- \
              env GITEA_WORK_DIR=/var/lib/gitea \
              GITEA_CUSTOM=/var/lib/gitea/custom \
              gitea admin user create \
              --config /var/lib/gitea/custom/conf/app.ini \
              --username gitea-admin \
              --password testpassword123 \
              --email admin@example.com \
              --admin \
              --must-change-password=false

            TOKEN=$(curl -fs -X POST \
              -H "Content-Type: application/json" \
              -d '{"name":"nixbot-token","scopes":["write:repository","write:user","write:organization"]}' \
              -u "gitea-admin:testpassword123" \
              http://localhost:3742/api/v1/users/gitea-admin/tokens | jq -r .sha1)

            mkdir -p /var/lib/secrets
            echo "$TOKEN" > /var/lib/secrets/gitea-token
            chmod 644 /var/lib/secrets/gitea-token
            cp /var/lib/secrets/gitea-token /tmp/gitea-token

            curl -fs -X POST \
              -H "Authorization: token $TOKEN" \
              -H "Content-Type: application/json" \
              -d '{"name":"test-flake","private":false,"default_branch":"master"}' \
              http://localhost:3742/api/v1/user/repos

            curl -fs -X PUT \
              -H "Authorization: token $TOKEN" \
              -H "Content-Type: application/json" \
              -d '{"topics":["nixbot"]}' \
              http://localhost:3742/api/v1/repos/gitea-admin/test-flake/topics

            rm -rf /tmp/test-flake
            ${setupTestFlake}
            git remote add origin http://gitea-admin:testpassword123@localhost:3742/gitea-admin/test-flake.git
            git push -u origin master
          '';
        };
      };
  };

  testScript = ''
    ${builtins.readFile ./github-helpers.py}

    start_all()

    with subtest("github: nixbot becomes healthy"):
        github.wait_for_unit("nixbot.service")
        github.wait_until_succeeds(
            "curl --fail -s http://127.0.0.1:8010/health", timeout=120
        )

    with subtest("github: project discovered from fake forge"):
        def github_project_discovered(_ignore):
            out = github.succeed("curl --fail -s http://127.0.0.1:8010/api/repos")
            projects = json.loads(out)
            print(projects)
            return any(
                p["owner"] == "acme" and p["name"] == "test-flake"
                for p in projects
            )

        retry(github_project_discovered, timeout_seconds=120)

    with subtest("github: webhook push triggers eval, build, and statuses"):
        push_webhook(github)

        def github_checks_posted(_ignore):
            done = completed_check_runs(github)
            return (
                done.get("nixbot/nix-eval") == "success"
                and done.get("nixbot/nix-build") == "success"
            )

        retry(github_checks_posted, timeout_seconds=300)

    with subtest("gitea: nixbot becomes healthy"):
        gitea.wait_for_unit("nixbot.service")
        # The gitea node keeps the default managed nginx vhost; the
        # engine only listens on the unix socket there, so probe
        # through nginx (the github node covers the plain TCP mode).
        gitea.wait_for_unit("nginx.service")
        gitea.wait_until_succeeds(
            "curl --fail -s http://localhost/health", timeout=120
        )

    with subtest("gitea: project discovered and webhook registered"):
        def gitea_hook_registered(_ignore):
            out = gitea.succeed(
                "TOKEN=$(cat /tmp/gitea-token); "
                "curl -fs -H \"Authorization: token $TOKEN\" "
                "http://localhost:3742/api/v1/repos/gitea-admin/test-flake/hooks"
            )
            hooks = json.loads(out)
            print(hooks)
            return any(h.get("active") for h in hooks)

        retry(gitea_hook_registered, timeout_seconds=180)

    with subtest("gitea: push triggers eval, build, and commit statuses"):
        gitea.succeed(
            "cd /tmp/test-flake && "
            "echo '# trigger' >> flake.nix && "
            "git add flake.nix && "
            "git commit -m 'trigger build' && "
            "git push origin master"
        )
        sha = gitea.succeed("git -C /tmp/test-flake rev-parse master").strip()

        def gitea_statuses_posted(_ignore):
            out = gitea.succeed(
                "TOKEN=$(cat /tmp/gitea-token); "
                "curl -fs -H \"Authorization: token $TOKEN\" "
                f"http://localhost:3742/api/v1/repos/gitea-admin/test-flake/statuses/{sha}"
            )
            statuses = json.loads(out)
            print(statuses)
            done = {
                s["context"]: s["status"]
                for s in statuses
                if s["status"] in ("success", "failure", "error")
            }
            return (
                done.get("nixbot/nix-eval") == "success"
                and done.get("nixbot/nix-build") == "success"
            )

        retry(gitea_statuses_posted, timeout_seconds=300)
  '';
}
