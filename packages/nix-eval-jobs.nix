# TODO: drop when nixpkgs ships nix-eval-jobs >= 2.35.1
{ pkgs, fetchFromGitHub }:
(pkgs.nix-eval-jobs.override {
  # nix-eval-jobs 2.35.x requires Nix >= 2.35 (tryEnterPrivateMountNamespace)
  nixComponents = pkgs.nixVersions.nixComponents_2_35;
}).overrideAttrs
  (
    finalAttrs: _prevAttrs: {
      version = "2.35.1";
      src = fetchFromGitHub {
        owner = "NixOS";
        repo = "nix-eval-jobs";
        tag = "v${finalAttrs.version}";
        hash = "sha256-EFJnN35L7UieL8zV8qPrpqfdfzztWksY8JYuXF+mr9o=";
      };
    }
  )
