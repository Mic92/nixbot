{
  self,
  inputs,
  lib,
  pkgs,
  ...
}:
let
  # this gives us a reference to our flake but also all flake inputs
  checkArgs = {
    inherit self pkgs;
  };
in
{
  treefmt = (inputs.treefmt-nix.lib.evalModule pkgs ../formatter/treefmt.nix).config.build.check self;
  nixbot-tests = self.packages.${pkgs.stdenv.hostPlatform.system}.nixbot.tests.pytest;
  nixbot-effects-tests = self.packages.${pkgs.stdenv.hostPlatform.system}.nixbot-effects.tests.pytest;
  effects-lib-ssh = import ./effects-lib-ssh.nix checkArgs;
  effects-lib-run-nixos = import ./effects-lib-run-nixos.nix checkArgs;
  sqlc-generated = import ./sqlc.nix checkArgs;
  effects-lib = import ./effects-lib.nix checkArgs;
  docs-examples = import ./docs-examples.nix checkArgs;
}
// lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
  nixbot = import ./nixbot.nix checkArgs;
  nixbot-gitlab = import ./nixbot-gitlab.nix checkArgs;
  nixbot-workload-identity = import ./nixbot-workload-identity.nix checkArgs;
}
