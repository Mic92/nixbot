{
  lib,
  config,
  pkgs,
  ...
}:
let
  cfg = config.services.nixbot;
in
{
  options.services.nixbot.niks3 = {
    enable = lib.mkEnableOption "Enable niks3 integration";

    serverUrl = lib.mkOption {
      type = lib.types.str;
      description = "niks3 server URL";
      example = "https://niks3.yourdomain.com";
    };

    authTokenFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to a file containing the niks3 API authentication token.
      '';
    };

    package = lib.mkOption {
      type = lib.types.package;
      description = "The niks3 package to use. You must add the niks3 flake input and overlay to make this package available.";
    };
  };

  config = lib.mkIf cfg.niks3.enable {
    systemd.services.nixbot.serviceConfig.LoadCredential = [
      "niks3-auth-token:${builtins.toString cfg.niks3.authTokenFile}"
    ];

    systemd.services.nixbot.path = [ cfg.niks3.package ];

    services.nixbot.uploaders = [
      (
        {
          name = "niks3";
          environment = {
            NIKS3_SERVER_URL = cfg.niks3.serverUrl;
            # Token via file, never on the command line: /proc/<pid>/cmdline
            # is world-readable.
            NIKS3_AUTH_TOKEN_FILE = "/run/credentials/nixbot.service/niks3-auth-token";
          };
        }
        // (
          # One long-running `niks3 push --stdin` (niks3 >= 1.11): each
          # attribute waits only for its own paths, not a shared batch.
          if lib.versionAtLeast (lib.getVersion cfg.niks3.package) "1.11" then
            {
              pathsVia = "stream";
              command = [
                "niks3"
                "push"
                "--stdin"
              ];
            }
          else
            {
              command = [
                "niks3"
                "push"
              ];
            }
        )
      )
    ];
  };
}
