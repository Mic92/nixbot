# flakelet-push from Mic92/flakelet-relay for the deploy effect, as a
# plain package so this flake needs no extra input.
{
  rustPlatform,
  fetchFromGitHub,
}:
rustPlatform.buildRustPackage {
  pname = "flakelet-push";
  version = "0-unstable-2026-09-03";
  src = fetchFromGitHub {
    owner = "Mic92";
    repo = "flakelet-relay";
    rev = "524322d75971302890969b941b2cbfe165d8aac3";
    hash = "sha256-iMP8MG/0iycvrQ1z43IRvQJc7/HldhcS+sM2NKqmoqs=";
  };
  cargoHash = "sha256-gjNXc8rQZM5QKcVu/TV5RJzLdhC8XVhtRICrq1ZtK+A=";
  cargoBuildFlags = [
    "--bin"
    "flakelet-push"
  ];
  doCheck = false;
  meta.mainProgram = "flakelet-push";
}
