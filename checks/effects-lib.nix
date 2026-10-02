# The shell helpers mkEffect gives an effect: its setup hook, sourced as
# in the effect sandbox and run against a secrets file.
{ pkgs, ... }:
let
  inherit (pkgs) lib;
  effects = import ../herculesCI/effects-lib.nix { inherit pkgs; };
  effect = effects.mkEffect {
    priorCheckScript = "echo prior >>$TMPDIR/log; false";
    putStateScript = "echo put >>$TMPDIR/log";
    effectCheckScript = "echo check >>$TMPDIR/log";
  };
  hook = lib.findFirst (
    p: lib.getName p == "hercules-ci-effect-sh"
  ) (throw "mkEffect has no setup hook") effect.nativeBuildInputs;
in
pkgs.runCommand "effects-lib-shell-helpers"
  {
    nativeBuildInputs = [
      pkgs.jq
      pkgs.openssh
    ];
  }
  ''
    export IN_HERCULES_CI_EFFECT=true HOME=$TMPDIR/home
    export HERCULES_CI_SECRETS_JSON=$TMPDIR/secrets.json
    cat >"$HERCULES_CI_SECRETS_JSON" <<'JSON'
    {
      "azure": { "data": { "tenant": "contoso" } },
      "ssh": { "data": { "privateKey": "PRIVATE", "publicKey": "PUBLIC" } },
      "hercules-ci": { "data": { "token": "task-token" } }
    }
    JSON
    source ${hook}/nix-support/setup-hook

    [[ $(readSecretString azure .tenant) == contoso ]]
    if readSecretString azure .missing 2>/dev/null; then
      echo "readSecretString found a missing path" >&2
      exit 1
    fi
    [[ $(readSecretJSON ssh .) == '{"privateKey":"PRIVATE","publicKey":"PUBLIC"}' ]]

    writeSSHKey ssh
    [[ $(cat ~/.ssh/id_rsa) == PRIVATE && $(cat ~/.ssh/id_rsa.pub) == PUBLIC ]]
    [[ $(stat -c %a ~/.ssh/id_rsa) == 400 ]]

    # getStateFile/putStateFile authenticate with the run's task token.
    initHerculesCIAPI
    [[ $(cat "$herculesCIHeaders") == "Authorization: Bearer task-token" ]]

    # Phases: a failing prior check does not stop the effect, and state is
    # uploaded once.
    runHook() { :; }
    priorCheckScript=${lib.escapeShellArg effect.priorCheckScript}
    putStateScript=${lib.escapeShellArg effect.putStateScript}
    effectCheckScript=${lib.escapeShellArg effect.effectCheckScript}
    eval ${lib.escapeShellArg effect.priorCheckPhase}
    eval ${lib.escapeShellArg effect.putStatePhase}
    eval ${lib.escapeShellArg effect.putStatePhase}
    eval ${lib.escapeShellArg effect.effectCheckPhase}
    [[ $(cat "$TMPDIR/log") == $'prior\nput\ncheck' ]]
    touch $out
  ''
