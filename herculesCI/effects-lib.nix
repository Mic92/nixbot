# Minimal subset of hercules-ci-effects sufficient for nixbot-effects.
# Avoids pulling hercules-ci-effects (and its transitive flake-parts input)
# into every consumer of this flake.
{ pkgs }:
let
  # Fetches a workload-identity ID token from nixbot inside the effect
  # sandbox, see docs/WORKLOAD_IDENTITY.md. --json prints the raw
  # {token, expires_at} response (niks3's ScriptToken format).
  idTokenScript = pkgs.writers.writePython3Bin "nixbot-id-token" { } (
    builtins.readFile ./nixbot-id-token.py
  );
in
{
  mkEffect =
    {
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
    }:
    pkgs.stdenvNoCC.mkDerivation {
      inherit
        name
        effectScript
        userSetupScript
        ;
      # Attr paths are nested lists, which cannot be coerced into
      # derivation env vars; expose them via passthru instead.
      passthru = { inherit after lock; };
      isEffect = true;
      __nixbot_effect_checkout = checkout;
      secretsMap = builtins.toJSON secretsMap;
      idTokenAudiences = builtins.toJSON idTokenAudiences;
      nativeBuildInputs = [
        pkgs.cacert
        pkgs.curl
        pkgs.jq
      ]
      ++ (if idTokenAudiences != [ ] then [ idTokenScript ] else [ ])
      ++ inputs;
      phases = [
        "initPhase"
        "userSetupPhase"
        "effectPhase"
      ];
      initPhase = ''
        exec </dev/null
        export HOME=/build/home
        mkdir -p "$HOME"
        echo "root:x:$(id -u):$(id -g):root:$HOME:/bin/sh" >> /etc/passwd
        mkdir -p ~/.ssh
        echo "BatchMode yes" >> ~/.ssh/config
      '';
      userSetupPhase = ''eval "$userSetupScript"'';
      effectPhase = ''eval "$effectScript"'';
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
}
