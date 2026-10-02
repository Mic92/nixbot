# Vendored verbatim from hercules-ci-effects
# (effects/nix-darwin/run-nix-darwin.nix at 6c58de7236d1cd634deea07bd15d52c7ce470bd3), so effects written
# for its `hci-effects.runNixDarwin` run unchanged.
#
# Copyright Hercules CI and contributors.
# Licensed under the Apache License, Version 2.0:
# https://github.com/hercules-ci/hercules-ci-effects/blob/master/LICENSE
{
  effects,
  mkEffect,
  lib,
  gnugrep,
  openssh,
  path,
}:

let
  inherit (lib) optionalAttrs;

  docUrl = "https://docs.hercules-ci.com/hercules-ci-effects/reference/nix-functions/runnixdarwin";

  isRequired =
    param:
    throw ''
      runNixDarwin: the argument ${param} wasn't specified. You must pass either:
       - nix-darwin, {nixpkgs or pkgs}, system and configuration,
       - or config

      See ${docUrl}
    '';
  mutExMsg = this: that: ''
    runNixDarwin: the argument ${this} is mutually exclusive with ${that}.

    See ${docUrl}
  '';
in

# See https://docs.hercules-ci.com/hercules-ci-effects/reference/nix-functions/runnixdarwin
# or docs/modules/ROOT/pages/reference/nix-functions/runNixDarwin.adoc
args@{
  # Configuration parameters
  nix-darwin ? isRequired "nix-darwin",
  configuration ? isRequired "configuration",
  nixpkgs ? null,
  pkgs ? null,
  system ? isRequired "system",
  config ? null,
  # Deployment parameters
  ssh,
  buildOnDestination ? null,
  # Misc, optional
  passthru ? { },
  ...
}:
let
  # An actual configuration object. If only `config` was passed, this will be incomplete.
  configuration_ =
    if
      args ? configuration.config
      && configuration ? _module
      # lenient: nix-darwin doesn't preserve the _type as of 2023-10
      && configuration._type or "configuration" == "configuration"
    then
      configuration

    else if
      args ? config.config
      && args ? config._module
      # lenient: nix-darwin doesn't preserve the _type as of 2023-10
      && config._type or "configuration" == "configuration"
    then
      lib.warn "hci-effects.runNixDarwin: `config` was renamed to `configuration` for the purpose of passing a whole configuration. Please pass your configuration in the `configuration` argument attribute." config

    else if args ? config.system.build.toplevel then
      { inherit (args) config; } # incomplete, but permissible for backcompat

    else if args ? configuration then # assume configuration is a module
      import nix-darwin (
        {
          inherit system;
          configuration =
            if args ? pkgs then
              {
                imports = [
                  { imports = [ configuration ]; }
                  # Apparent bug in nix-darwin; we need this
                  { _module.args.pkgs = lib.mkForce pkgs; }
                ];
              }
            else
              configuration;
        }
        // lib.filterAttrs (k: v: v != null) { inherit pkgs nixpkgs; }
        // lib.optionalAttrs (args ? pkgs && !args ? nixpkgs) { nixpkgs = pkgs.path; }
      )

    else
      throw "hci-effects.runNixDarwin: you must provide a configuration";

in
let
  configuration = configuration_;
  config = configuration.config;

  suffix =
    if args ? name then
      "-${args.name}"
    else if config.networking.hostName or null == null then
      ""
    else
      "-${config.networking.hostName}";

  toplevel = config.system.build.toplevel;

  profilePath = config.system.profile;

  mutEx = this: that: lib.throwIf (args ? ${this} && args ? ${that}) (mutExMsg this that);

  # Add default value for destinationPkgs, for when buildOnDestination is true
  ssh' = {
    destinationPkgs =
      configuration._module.args.pkgs or
      # before nix-darwin#723 (?)
      configuration.pkgs or (throw ''
        When `buildOnDestination` is true, you must either specify a whole nix-darwin configuration attrset (not just `config = myConfiguration.config`, or you must specify `ssh.destinationPkgs`.

        See also https://docs.hercules-ci.com/hercules-ci-effects/reference/nix-functions/ssh.html#param-buildOnDestination
      '');
  }
  // ssh
  // optionalAttrs (buildOnDestination != null) {
    inherit buildOnDestination;
  };

in

mutEx "config" "configuration" mutEx "config" "nixpkgs" mutEx "config" "nix-darwin" mutEx "config"
  "system"
  mutEx
  "config"
  "pkgs"

  mkEffect
  (
    removeAttrs args [
      "configuration"
      "ssh"
      "config"
      "system"
      "nix-darwin"
      "nixpkgs"
      "pkgs"
      "buildOnDestination"
    ]
    // {
      name = "nix-darwin${suffix}";
      inputs = [
        gnugrep
        openssh
      ];
      dontUnpack = true;
      passthru = passthru // {
        prebuilt = toplevel // {
          inherit config;
        };
        inherit config;
      };
      effectScript = ''
        ${effects.ssh ssh' ''
          set -eu
          echo >&2 "remote nix version:"
          nix-env --version >&2
          if
            [[ -x ${toplevel}/activate-user ]] \
            && ! grep -q '^# nix-darwin: deprecated$' ${toplevel}/activate-user
          then
            if [ "$USER" != root ] && [ ! -w $(dirname "${profilePath}") ]; then
              sudo -H nix-env -p ${profilePath} --set ${toplevel}
            else
              nix-env -p ${profilePath} --set ${toplevel}
            fi
            ${toplevel}/sw/bin/darwin-rebuild activate
          else
            if [ "$USER" != root ] && [ ! -w $(dirname "${profilePath}") ]; then
              sudo -H nix-env -p ${profilePath} --set ${toplevel}
              sudo -H ${toplevel}/sw/bin/darwin-rebuild activate
            else
              nix-env -p ${profilePath} --set ${toplevel}
              ${toplevel}/sw/bin/darwin-rebuild activate
            fi
          fi
        ''}
      '';
    }
  )
