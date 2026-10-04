# Vendored verbatim from hercules-ci-effects
# (effects/nixos/run-nixos.nix at 6c58de7236d1cd634deea07bd15d52c7ce470bd3), so effects written
# for its `hci-effects.runNixOS` run unchanged.
#
# Copyright Hercules CI and contributors.
# Licensed under the Apache License, Version 2.0:
# https://github.com/hercules-ci/hercules-ci-effects/blob/master/LICENSE
{
  nixos,
  effects,
  lib,
  mkEffect,
  openssh,
  nix,
  path,
}:
let
  inherit (lib) optionalAttrs isAttrs;
in
args@{
  configuration ? throw "effects.runNixOS: you must provide a configuration (or a fully evaluated configuration in `config`)",
  # null means let the configuration specify it
  system ? null,
  nixpkgs ? path,
  config ? null,
  profile ? "/nix/var/nix/profiles/system",
  ssh,
  passthru ? { },
  buildOnDestination ? null,
  ...
}:
let
  # An actual configuration object. If only `config` was passed, this will be incomplete.
  configuration_ =
    if
      args ? configuration.config
      && configuration ? _module
      && configuration._type or null == "configuration"
    then
      configuration
    else if args ? config then
      { inherit (args) config; } # incomplete, but permissible for backcompat
    else if args ? configuration then
      import (nixpkgs + "/nixos/lib/eval-config.nix") {
        modules = [ configuration ];
        inherit system;
      }
    else
      throw "effects.runNixOS: you must provide a configuration";

  config = configuration_.config;

  ssh' = {
    inherit destinationPkgs;
  }
  // ssh
  // optionalAttrs (buildOnDestination != null) {
    inherit buildOnDestination;
  };

  # Only evaluated when ssh'.buildOnDestination = true.
  # Ideally this whole thing goes away when Nix gives good access to a string's
  # derivation paths in pure mode.
  destinationPkgs =
    configuration_._module.args.pkgs or (throw ''
      runNixOS: ssh.destinationPkgs is required when
        - invoking effects.runNixOS with just a config parameter
        - and ssh.buildOnDestination = true;

      Ideally you could pass a whole NixOS configuration object instead of just the config attribute.
      Consider changing your invocation to match one of the following examples,
      whichever is easiest for you:

          runNixOS {
            configuration = self.nixosConfigurations.foo;
            # or
            # configuration = lib.nixosSystem ./configuration.nix;
            # or
            # configuration = import (nixpkgs + "/nixos/lib/eval-config.nix") { ... };
          }

      Or let runNixOS perform the NixOS invocation for you, e.g.

          runNixOS {
            configuration = ./configuration.nix;
          }

      Or pass a suitable pkgs, buildable on the destination, by hand:

          runNixOS {
            ssh = {
              # ...

              destinationPkgs = nixpkgs.legacyPackages.x86_64-linux;
              # or e.g.
              # destinationPkgs = import nixpkgs { system = "x86_64-linux"; };
            };
          }
    '');

  checked =
    if !(config ? environment.systemPackages) then
      throw "effects.runNixOS expects `config` to be an already evaluated configuration, like the `config` variable that's used in NixOS modules. Perhaps you intended to write `configuration` instead of `config`?"
    else
      x: x;
  inherit (config.system.build) toplevel;
in
checked (
  mkEffect (
    removeAttrs args [
      "config"
      "configuration"
      "system"
      "nixpkgs"
      "ssh"
      "buildOnDestination"
    ]
    // {
      name = "nixos-${ssh.destination}";
      inputs = [
        # For user setup
        openssh
      ];
      effectScript = ''
        ${args.effectScript or ""}
        ${effects.ssh ssh' ''
          set -euo pipefail
          echo >&2 "remote nix version:"
          nix-env --version >&2
          nix-env -p ${profile} --set ${toplevel}
          ${toplevel}/bin/switch-to-configuration switch
        ''}
      '';
      passthru = {
        prebuilt = toplevel // {
          inherit config;
        };
        inherit config;
      }
      // passthru;
      dontUnpack = args.dontUnpack or true;
    }
  )
)
