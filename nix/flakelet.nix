{ types, ... }:
{
  options = {
    config = {
      type = types.attrsOf types.any;
      description = "nixbot configuration, written as nixbot-config.json.";
    };
    listen = {
      type = types.option (
        types.union [
          types.string
          types.number
        ]
      );
      description = "Unix socket path or TCP port; defaults to /run/<name>/web.sock.";
    };
    user = {
      type = types.string;
      default = "nixbot";
    };
    domain = {
      type = types.option types.string;
      description = "Publish an http contract export for this vhost.";
    };
    credentials = {
      type = types.attrsOf types.string;
      default = { };
      description = "systemd LoadCredential id to host path.";
    };
    after = {
      type = types.listOf types.string;
      default = [ ];
    };
    requires = {
      type = types.listOf types.string;
      default = [ ];
    };
  };

  impl =
    {
      options,
      pkgs,
      name,
      contracts,
      ...
    }:
    let
      inherit (pkgs) lib;
      nix-eval-jobs = pkgs.callPackage ../packages/nix-eval-jobs.nix { };
      nixbot = pkgs.python3.pkgs.callPackage ../packages/nixbot.nix { inherit nix-eval-jobs; };
      configFile = (pkgs.formats.json { }).generate "nixbot-config.json" options.config;
      listen = if options.listen == null then "/run/${name}/web.sock" else options.listen;
      overUnixSocket = !lib.isInt listen;
      user = options.user;
      gcrootsDir = options.config.gcroots_dir or "/nix/var/nix/gcroots/per-user/${user}";
      outputsPath = options.config.outputs_path or null;
      tmpfilesConf = pkgs.writeText "${name}-tmpfiles.conf" (
        ''
          d ${gcrootsDir} 0755 ${user} ${user} - -
          e ${gcrootsDir}/* - - - 90d -
        ''
        + lib.optionalString (outputsPath != null) ''
          d ${outputsPath} 0755 ${user} ${user} - -
        ''
      );
      tmpfiles = "${pkgs.systemd}/bin/systemd-tmpfiles";
    in
    {
      sockets.${name} = {
        description = "nixbot CI service socket";
        wantedBy = [ "sockets.target" ];
        socketConfig = {
          ListenStream = listen;
          SocketUser = user;
          SocketGroup = user;
          SocketMode = "0660";
          # Owned by the socket unit so a service stop does not remove the path.
          RuntimeDirectory = name;
        };
      };

      services.${name} = {
        description = "nixbot CI service";
        wantedBy = [ "multi-user.target" ];
        after = [
          "network-online.target"
          "${name}.socket"
        ]
        ++ options.after;
        wants = [ "network-online.target" ];
        requires = [ "${name}.socket" ] ++ options.requires;

        path = [
          pkgs.git
          pkgs.openssh
          pkgs.openssl
          pkgs.bash
          pkgs.coreutils
          pkgs.bubblewrap
          nix-eval-jobs
          pkgs.nix
        ];

        # Remote builders need a HOME for ~/.ssh.
        environment.HOME = "/var/lib/${name}";

        serviceConfig = {
          # "+": root creates the gcroots/outputs directories.
          ExecStartPre = "+${tmpfiles} --create ${tmpfilesConf}";
          ExecStart = "${lib.getExe' nixbot "nixbot"} --config ${configFile}";
          # Readiness gate: a failed start job rolls the activation back.
          ExecStartPost = pkgs.writeShellScript "${name}-health" ''
            for _ in $(seq 60); do
              if ${pkgs.curl}/bin/curl -fsS --max-time 5 \
                ${
                  if overUnixSocket then
                    ''--unix-socket ${listen} "http://localhost/health"''
                  else
                    ''"http://127.0.0.1:${toString listen}/health"''
                } >/dev/null; then
                exit 0
              fi
              sleep 1
            done
            echo "nixbot did not become healthy" >&2
            exit 1
          '';
          # The health loop can take ~360s, e.g. during long migrations.
          TimeoutStartSec = 600;
          User = user;
          Group = user;
          StateDirectory = name;
          StateDirectoryMode = "0700";
          RuntimeDirectory = name;
          # The socket unit shares the runtime dir with the listener path.
          RuntimeDirectoryPreserve = true;
          WorkingDirectory = "/var/lib/${name}";
          Restart = "on-failure";
          RestartSec = 5;
          # Let nixbot cancel builds and reap nix children itself on stop.
          KillMode = "mixed";
          TimeoutStopSec = 180;

          LoadCredential = lib.mapAttrsToList (id: path: "${id}:${path}") options.credentials;

          # Hardening; unprivileged bwrap sandboxes need namespaces and mounts.
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          PrivateDevices = true;
          Delegate = "memory";
          ProtectControlGroups = false;
          ProtectKernelModules = true;
          # /proc overmounts break bwrap's fresh proc mount.
          ProtectKernelTunables = false;
          ProtectKernelLogs = false;
          ProtectClock = true;
          ProtectProc = "default";
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          LockPersonality = true;
          CapabilityBoundingSet = "";
          RestrictAddressFamilies = [
            "AF_UNIX"
            "AF_INET"
            "AF_INET6"
          ];
          RestrictNamespaces = "user mnt pid net ipc uts cgroup";
          SystemCallArchitectures = "native";
          SystemCallFilter = [
            "@system-service"
            "@mount"
            "unshare"
            "setns"
            "sethostname"
          ];
          ReadWritePaths = [ gcrootsDir ] ++ lib.optional (outputsPath != null) outputsPath;
        };
      };

      # The global systemd-tmpfiles-clean.timer only reads /etc/tmpfiles.d.
      timers.gcroots-clean = {
        description = "clean stale ${name} gc-roots";
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnCalendar = "daily";
          Persistent = true;
        };
      };
      services.gcroots-clean = {
        description = "clean stale ${name} gc-roots";
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${tmpfiles} --clean ${tmpfilesConf}";
        };
      };

      exports =
        lib.optionalAttrs (options.domain != null) {
          http.web = contracts.http {
            host = options.domain;
            upstream = "unix:${listen}";
            # Webhook payloads (GitHub caps at 25 MB); long timeout for SSE log
            # streams, which buffering would stall.
            maxBodySize = "25m";
            readTimeout = "3600s";
            buffering = false;
            extra.nginx = ''
              proxy_connect_timeout 120s;
                proxy_send_timeout 120s;
            ''
            + lib.optionalString (outputsPath != null) ''
              location /nix-outputs/ {
                  alias ${lib.removeSuffix "/" outputsPath}/;
                  charset utf-8;
                  autoindex on;
                }
            '';
          };
        }
        // lib.optionalAttrs (lib.hasInfix "host=/run/postgresql" (options.config.db_url or "")) {
          requires.postgres.database = user;
        };
    };
}
