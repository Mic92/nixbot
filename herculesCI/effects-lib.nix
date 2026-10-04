# Minimal subset of hercules-ci-effects sufficient for nixbot-effects.
# Avoids pulling hercules-ci-effects (and its transitive flake-parts input)
# into every consumer of this flake.
{
  pkgs,
  lib ? pkgs.lib,
}:
let
  # Fetches a workload-identity ID token from nixbot inside the effect
  # sandbox, see docs/WORKLOAD_IDENTITY.md. --json prints the raw
  # {token, expires_at} response (niks3's ScriptToken format).
  idTokenScript = pkgs.writers.writePython3Bin "nixbot-id-token" { } (
    builtins.readFile ./nixbot-id-token.py
  );
  # Posts/updates a PR comment from an onEvent effect, see docs/EFFECTS.md.
  prCommentScript = pkgs.writers.writePython3Bin "nixbot-pr-comment" { } (
    builtins.readFile ./nixbot-pr-comment.py
  );
  # hercules-ci-effects' shell functions (readSecretString, writeSSHKey,
  # getStateFile, ...). Same derivation name as upstream's.
  setupHook = pkgs.runCommand "hercules-ci-effect-sh" { } ''
    mkdir -p $out/nix-support
    # The headers file holds the task token. Upstream puts it in $PWD, which
    # is the repository clone with checkout = true; $TMPDIR is /build.
    sed 's|\$PWD/hercules-ci.headers|$TMPDIR/hercules-ci.headers|' \
      ${./effects-setup-hook.sh} >$out/nix-support/setup-hook
  '';
in
lib.fix (effects: {
  mkEffect =
    args@{
      effectScript ? "",
      userSetupScript ? "",
      name ? "effect",
      inputs ? [ ],
      secretsMap ? { },
      # Pushable repository checkout at /build/checkout ($NIXBOT_EFFECT_CHECKOUT),
      # see docs/EFFECTS.md.
      checkout ? false,
      # Audiences this effect may request workload-identity ID tokens
      # for via `nixbot-id-token <audience>`, see docs/WORKLOAD_IDENTITY.md.
      idTokenAudiences ? [ ],
      # Scheduling metadata read via `nixbot-effects list`: attribute paths
      # of effects that must succeed first, job name first
      # (e.g. [ [ "default" "deploy-staging" ] ]), and a named lock
      # serializing runs across builds.
      after ? [ ],
      lock ? null,
      # onEvent only: conditions nixbot checks against the event before
      # running, see docs/EFFECTS.md. `lock` may contain `{pr}` there.
      when ? { },
      # Phase scripts and the extra phases around them, as in
      # hercules-ci-effects' mkEffect.
      getStateScript ? "",
      putStateScript ? "",
      priorCheckScript ? "",
      effectCheckScript ? "",
      preGetStatePhases ? "",
      preEffectPhases ? "priorCheckPhase",
      postEffectPhases ? "effectCheckPhase",
      passthru ? { },
      # Like upstream's mkEffect, any other attribute goes to mkDerivation
      # (e.g. runNixOS sets dontUnpack and passthru.prebuilt).
      ...
    }:
    pkgs.stdenvNoCC.mkDerivation (
      removeAttrs args [
        "inputs"
        "checkout"
        "idTokenAudiences"
        "after"
        "lock"
        "when"
      ]
      // {
        inherit
          name
          effectScript
          userSetupScript
          getStateScript
          putStateScript
          priorCheckScript
          effectCheckScript
          ;
        # Attr paths are nested lists, which cannot be coerced into
        # derivation env vars; expose them via passthru instead.
        passthru = passthru // {
          inherit after lock when;
        };
        isEffect = true;
        __nixbot_effect_checkout = checkout;
        # like upstream hercules-ci-effects
        __hci_effect_fsroot_copy = pkgs.runCommand "mkEffect-root" { } ''
          mkdir -p $out/bin $out/usr/bin
          ln -s ${lib.getExe pkgs.bash} $out/bin/sh
          ln -s ${pkgs.coreutils}/bin/env $out/usr/bin/env
        '';
        secretsMap = builtins.toJSON secretsMap;
        idTokenAudiences = builtins.toJSON idTokenAudiences;
        nativeBuildInputs = [
          setupHook
          pkgs.cacert
          pkgs.curl
          pkgs.jq
          prCommentScript
        ]
        ++ (if idTokenAudiences != [ ] then [ idTokenScript ] else [ ])
        ++ inputs;
        phases = lib.splitString " " (
          lib.concatStringsSep " " (
            lib.filter (p: p != "") [
              "initPhase"
              preGetStatePhases
              "getStatePhase"
              "userSetupPhase"
              preEffectPhases
              "effectPhase"
              "putStatePhase"
              postEffectPhases
            ]
          )
        );
        initPhase = ''
          exec </dev/null
          # The setup hook prepares the state API's credentials here.
          runHook preInit
          export HOME=/build/home
          mkdir -p "$HOME"
          echo "root:x:$(id -u):$(id -g):root:$HOME:/bin/sh" >> /etc/passwd
          mkdir -p ~/.ssh
          echo "BatchMode yes" >> ~/.ssh/config
          runHook postInit
        '';
        getStatePhase = ''
          runHook preGetState
          eval "$getStateScript"
          runHook postGetState
          registerPutStatePhaseOnFailure
        '';
        userSetupPhase = ''
          runHook preUserSetup
          eval "$userSetupScript"
          runHook postUserSetup
        '';
        # A failing check must not stop the effect: it may fix the problem.
        priorCheckPhase = ''
          runHook prePriorCheck
          if [[ -n "$priorCheckScript" ]] && ! eval "$priorCheckScript"; then
            echo 1>&2 "WARNING: prior check failed, continuing"
          fi
          runHook postPriorCheck
        '';
        effectPhase = ''
          runHook preEffect
          eval "$effectScript"
          runHook postEffect
        '';
        # Runs on failure too, see registerPutStatePhaseOnFailure.
        putStatePhase = ''
          if [[ -z ''${PUT_STATE_DONE:-} ]]; then
            runHook prePutState
            eval "$putStateScript"
            runHook postPutState
            PUT_STATE_DONE=true
          fi
        '';
        effectCheckPhase = ''
          runHook preEffectCheck
          eval "$effectCheckScript"
          runHook postEffectCheck
        '';
      }
    );

  # Runs a script on a host over ssh, like hercules-ci-effects' `ssh`.
  # See docs/EFFECTS.md.
  ssh = pkgs.callPackage ./call-ssh.nix { };

  # hercules-ci-effects' `runNixOS` and `runNixDarwin`: switch a host to an
  # evaluated configuration over `ssh`, e.g. `effects.runNixOS { configuration
  # = self.nixosConfigurations.foo; ssh.destination = "root@foo"; }`.
  runNixOS = pkgs.callPackage ./run-nixos.nix {
    inherit effects;
    inherit (effects) mkEffect;
  };
  runNixDarwin = pkgs.callPackage ./run-nix-darwin.nix {
    inherit effects;
    inherit (effects) mkEffect;
  };

  # When the condition is false we still want eval/build of the effect's
  # closure to succeed, so return a no-op effect instead.
  runIf =
    condition: effect:
    if condition then
      { run = effect; }
    else
      {
        dependencies = effect.inputDerivation // {
          isEffect = false;
          buildDependenciesOnly = true;
        };
      };
})
