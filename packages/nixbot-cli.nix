{
  buildPythonPackage,
  callPackage,
  hatchling,
  httpx,
  bubblewrap,
  stdenv,
  lib,
}:
let
  nixbot-effects = callPackage ./nixbot-effects.nix { };
in
buildPythonPackage {
  name = "nixbot-cli";
  pyproject = true;
  src = ./../nixbot_cli;
  build-system = [ hatchling ];
  dependencies = [
    httpx
    nixbot-effects
  ];

  # `nbo effects run` sandboxes the effect with bwrap. The sandbox only
  # exists on Linux and bubblewrap does not evaluate on Darwin.
  makeWrapperArgs = lib.optionals stdenv.hostPlatform.isLinux [
    "--prefix PATH : ${lib.makeBinPath [ bubblewrap ]}"
  ];

  # The CLI tests live in the server's suite (nixbot.passthru.tests.pytest)
  # because they run against the real web app.

  meta = {
    description = "Command-line client (nbo) for the nixbot CI service";
    homepage = "https://github.com/nix-community/nixbot";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.mic92 ];
    mainProgram = "nbo";
  };
}
