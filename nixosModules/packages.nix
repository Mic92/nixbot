{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.services.nixbot.packages;
in
{
  options.services.nixbot.packages = {
    python = lib.mkOption {
      type = lib.types.package;
      default = pkgs.python3;
      defaultText = lib.literalExpression "pkgs.python3";
      description = "Python interpreter to use for nixbot.";
    };

    nix-eval-jobs = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../packages/nix-eval-jobs.nix { };
      defaultText = lib.literalExpression "pkgs.nix-eval-jobs";
      description = "The nix-eval-jobs package to use.";
    };

    nixbot = lib.mkOption {
      type = lib.types.package;
      default = cfg.python.pkgs.callPackage ../packages/nixbot.nix {
        inherit (cfg) nix-eval-jobs;
      };
      defaultText = lib.literalExpression "python.pkgs.callPackage ../packages/nixbot.nix { }";
      description = "The nixbot package to use.";
    };
  };
}
