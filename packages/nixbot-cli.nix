{
  buildPythonPackage,
  hatchling,
  httpx,
  lib,
}:
buildPythonPackage {
  name = "nixbot-cli";
  pyproject = true;
  src = ./../nixbot_cli;
  build-system = [ hatchling ];
  dependencies = [ httpx ];

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
