{
  self,
  pkgs,
  system,
  ...
}:
let
  # process-compose wrapped with the local dev stack config
  # (postgres + nixbot).
  devProcessCompose = pkgs.python3.pkgs.callPackage ../packages/process-compose.nix {
    nixbot = self.packages.${system}.nixbot;
    inherit (self.packages.${system}) nix-eval-jobs;
  };
  # sqlc wrapped with the nix-pinned codegen plugin (same version as
  # the sqlc-generated flake check); works offline.
  sqlc = import ../nix/sqlc.nix { inherit pkgs; };
in
{
  default = pkgs.mkShell {
    packages = [
      pkgs.bashInteractive
      pkgs.mypy
      pkgs.ruff
      pkgs.postgresql
      self.packages.${system}.nix-eval-jobs
      # SQL codegen: `sqlc generate` regenerates nixbot/nixbot/db_gen
      # from sqlc.yaml.
      sqlc
      devProcessCompose
      # nbo, the CLI client
      self.packages.${system}.nixbot-cli
      (pkgs.python3.withPackages (
        ps:
        [
          ps.pytest
          ps.pytest-asyncio
          ps.pytest-timeout
          ps.pytest-xdist
          ps.pytest-benchmark
          ps.playwright
          ps.pyte
        ]
        ++ self.packages.${system}.nixbot.dependencies
      ))
    ];
    # pkgs.mypy's setup hook disables pytest plugin autoloading, which
    # silently turns off pytest-timeout and pytest-xdist.
    shellHook = ''
      unset PYTEST_DISABLE_PLUGIN_AUTOLOAD
    '';
    env = {
      PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
      PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
    };
  };
}
