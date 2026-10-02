# effects-lib's `runNixOS`: evaluating it against a small NixOS
# configuration yields an effect that targets the host and deploys that
# configuration's toplevel. Evaluation only, so the check stays cheap.
{ pkgs, ... }:
let
  inherit (pkgs) lib;
  effects = import ../herculesCI/effects-lib.nix { inherit pkgs; };
  configuration = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    system = null; # taken from nixpkgs.hostPlatform below
    modules = [
      {
        # Evaluated only, never built, so any platform works on any system.
        nixpkgs.hostPlatform = "x86_64-linux";
        boot.isContainer = true;
        system.stateVersion = lib.trivial.release;
      }
    ];
  };
  effect = effects.runNixOS {
    inherit configuration;
    ssh.destination = "root@rig";
  };
in
assert effect.isEffect;
# mkDerivation sanitises the "@" of "nixos-root@rig".
assert effect.name == "nixos-root-rig";
# Attributes mkEffect does not know reach mkDerivation, as with upstream.
assert effect.drvAttrs.dontUnpack;
assert effect.prebuilt.outPath == configuration.config.system.build.toplevel.outPath;
assert lib.isFunction effects.runNixDarwin;
pkgs.runCommand "effects-lib-run-nixos" { } "touch $out"
