{
  hatchling,
  buildPythonPackage,
}:
buildPythonPackage {
  name = "nixbot-effects";
  pyproject = true;
  src = ./../nixbot_effects;
  build-system = [
    hatchling
  ];
}
