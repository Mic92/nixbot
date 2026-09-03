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
    rev = "521f8072bcdcfca712d5598a9979d5b7557eab72";
    hash = "sha256-3fLTA1nRvw7zZilI9mEeuqOvGEW8+VeE+82K7jdhaJw=";
  };
  cargoHash = "sha256-gjNXc8rQZM5QKcVu/TV5RJzLdhC8XVhtRICrq1ZtK+A=";
  cargoBuildFlags = [
    "--bin"
    "flakelet-push"
  ];
  doCheck = false;
  meta.mainProgram = "flakelet-push";
}
