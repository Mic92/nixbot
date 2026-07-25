{
  lib,
  stdenv,
  bubblewrap,
  hatchling,
  buildPythonApplication,
}:
buildPythonApplication {
  name = "nixbot-effects";
  pyproject = true;
  src = ./../nixbot_effects;
  build-system = [
    hatchling
  ];
  # The bwrap sandbox only exists on Linux and bubblewrap does not
  # evaluate on Darwin.
  makeWrapperArgs = lib.optionals stdenv.hostPlatform.isLinux [
    "--prefix PATH : ${lib.makeBinPath [ bubblewrap ]}"
  ];
}
