# Shared github-mode node for the NixOS tests: nixbot against a fake
# GitHub API, with a bare test repository at /var/lib/test-repo built
# from `flakeText` (a function of pkgs, so it can embed store paths).
# Check runs posted by nixbot are appended to
# /var/lib/fake-github/check_runs.jsonl for the test scripts to assert
# on.
{ flakeText }:
{ self, pkgs, ... }:
let
  fakeGithubPort = 8970;
  fakeGithub = pkgs.writers.writePython3Bin "fake-github" { } ''
    import json
    import re
    from http.server import BaseHTTPRequestHandler, HTTPServer

    CHECK_RUNS_LOG = "/var/lib/fake-github/check_runs.jsonl"

    REPO = {
        "id": 1,
        "name": "test-flake",
        "owner": {"login": "acme"},
        "default_branch": "master",
        "clone_url": "file:///var/lib/test-repo",
        "private": False,
        "topics": ["build-with-buildbot"],
    }


    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload, code=200):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/app/installations":
                self._json([{"id": 1}])
            elif path == "/installation/repositories":
                self._json({"repositories": [REPO]})
            elif path == "/repos/acme/test-flake/pulls":
                # Reconciliation polls open PRs; none exist here.
                self._json([])
            else:
                self._json({"message": "not found"}, 404)

        def do_PATCH(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            path = self.path.split("?")[0]
            if re.fullmatch(r"/repos/acme/test-flake/check-runs/[0-9]+", path):
                entry = json.loads(body)
                with open(CHECK_RUNS_LOG, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                self._json({"id": int(path.rsplit("/", 1)[1])}, 200)
            else:
                self._json({"message": "not found"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            path = self.path.split("?")[0]
            if re.fullmatch(r"/app/installations/1/access_tokens", path):
                self._json({"token": "fake-token"}, 201)
            elif path == "/repos/acme/test-flake/check-runs":
                entry = json.loads(body)
                # The poster stores this id to PATCH the run on
                # completion; key it by name so each check keeps a
                # distinct id like the real API.
                check_run_id = abs(hash(entry["name"])) % 1_000_000
                with open(CHECK_RUNS_LOG, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                self._json({"id": check_run_id}, 201)
            else:
                self._json({"message": "not found"}, 404)

        def log_message(self, fmt, *args):
            pass


    HTTPServer(("127.0.0.1", ${toString fakeGithubPort}), Handler).serve_forever()
  '';
  testFlake = pkgs.writeText "flake.nix" (flakeText pkgs);
in
{
  imports = [ self.nixosModules.nixbot ];

  services.nixbot = {
    enable = true;
    domain = "localhost";
    nginx.enable = false;
    github = {
      enable = true;
      appId = 123;
      apiUrl = "http://127.0.0.1:${toString fakeGithubPort}";
      appSecretKeyFile = "/var/lib/secrets/github-app-key.pem";
      webhookSecretFile = pkgs.writeText "webhook-secret" "test-webhook-secret";
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

  systemd.services.fake-github = {
    wantedBy = [ "multi-user.target" ];
    before = [ "nixbot.service" ];
    requiredBy = [ "nixbot.service" ];
    serviceConfig = {
      ExecStart = "${fakeGithub}/bin/fake-github";
      StateDirectory = "fake-github";
    };
  };

  systemd.services.setup-test-repo = {
    wantedBy = [ "multi-user.target" ];
    before = [ "nixbot.service" ];
    requiredBy = [ "nixbot.service" ];
    path = [
      pkgs.git
      pkgs.openssl
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      mkdir -p /var/lib/secrets
      openssl genrsa -out /var/lib/secrets/github-app-key.pem 2048
      chmod 644 /var/lib/secrets/github-app-key.pem

      rm -rf /var/lib/test-repo /tmp/test-flake
      mkdir -p /tmp/test-flake
      cd /tmp/test-flake
      git init -b master
      git config user.name test
      git config user.email test@example.com
      cp ${testFlake} flake.nix
      git add flake.nix
      git commit -m "initial commit"
      git clone --bare /tmp/test-flake /var/lib/test-repo
      chmod -R a+rX /var/lib/test-repo
    '';
  };
}
