# TODO: drop when nixpkgs ships nix-eval-jobs >= 2.35.1
{ pkgs, fetchFromGitHub }:
let
  # boost.context fixes needed by nix 2.35 coroutines (NixOS/nix#16174).
  # Newer nixpkgs already carry them (detected via the second commit).
  boostHasContextFix = builtins.any (
    p: pkgs.lib.hasInfix "5883212311535a0046031d74d1568ae173c1e35b" (toString p)
  ) (pkgs.boost.patches or [ ]);
  boostPatched =
    if
      boostHasContextFix
      || !(
        pkgs.lib.versionAtLeast pkgs.boost.version "1.88" && pkgs.lib.versionOlder pkgs.boost.version "1.93"
      )
    then
      pkgs.boost
    else
      pkgs.boost.overrideAttrs (prevAttrs: {
        patches = (prevAttrs.patches or [ ]) ++ [
          (pkgs.fetchpatch {
            url = "https://github.com/boostorg/context/commit/0921b9fd5c776aec7748475c6c10807e0d51bc6d.patch";
            relative = "include";
            hash = "sha256-nQYMd3HFsDLxijnGdyas0ZHs3ylQVMGQL14K7F6MkF0=";
          })
          (pkgs.fetchpatch {
            url = "https://github.com/boostorg/context/commit/5883212311535a0046031d74d1568ae173c1e35b.patch";
            relative = "include";
            hash = "sha256-CytNLi2d0wjI/lY5lDv98mwwQaEt7qeIs4UkE6QgCBU=";
          })
        ];
      });

  # nix 2.35 eval perf regression: srcToStore cache not populated on
  # fetcher cache hits (NixOS/nix#16190, fixed after 2.35.2). Applied
  # only for nix < 2.36; patch fails if already included upstream.
  srcToStoreCachePatch = pkgs.fetchpatch {
    url = "https://github.com/NixOS/nix/commit/30820a54b112f4842bdb7df28b61b2a607e54033.patch";
    hash = "sha256-Yvn9a059LvW9FkSGH20LRPlBIhmVqQxGMBXke+hxkgs=";
  };

  # `nix flake prefetch-inputs` randomly skipped inputs, so the eval
  # sandbox then lacked them (#153, NixOS/nix#16373).
  prefetchInputsDedupPatch = pkgs.fetchpatch {
    url = "https://github.com/Mic92/nix-1/commit/e9bc07a77ddb16de5d47738e6f4b9e0188d7ed7a.patch";
    hash = "sha256-3QBE/cmolXVFcak1T49fC2vftb9CJyUsoJ4Z7WDqVJ4=";
  };

  patchIfNeeded =
    components:
    components.appendPatches (
      pkgs.lib.optional (pkgs.lib.versionOlder components.version "2.36") srcToStoreCachePatch
      ++ [ prefetchInputsDedupPatch ]
    );

  nixComponents = patchIfNeeded nixComponents_2_35;

  # polyfill for nixpkgs without nix 2.35 (e.g. stable release branches)
  nixComponents_2_35 =
    pkgs.nixVersions.nixComponents_2_35 or (
      (pkgs.nixVersions.nixComponents_2_34.overrideSource (fetchFromGitHub {
        owner = "NixOS";
        repo = "nix";
        tag = "2.35.2";
        hash = "sha256-C/YEm/5IPiAMxQH5aHlkwgQMkLqK7NVsudEWdlzBZAA=";
      })).overrideScope
      (
        _finalScope: _prevScope: {
          version = "2.35.2";
          boost = boostPatched;
        }
      )
    );
in
(pkgs.nix-eval-jobs.override {
  # nix-eval-jobs 2.35.x requires Nix >= 2.35 (tryEnterPrivateMountNamespace)
  inherit nixComponents;
}).overrideAttrs
  (
    _finalAttrs: prevAttrs: {
      # unreleased main: memory budget scheduler, per-attribute warnings and stats
      version = "2.35.2-unstable-2026-09-01";
      buildInputs = (prevAttrs.buildInputs or [ ]) ++ [ pkgs.mimalloc ];
      # The nix CLI nixbot runs (flake prefetch-inputs/archive) must carry
      # the same patches, so expose it alongside nix-eval-jobs.
      passthru = (prevAttrs.passthru or { }) // {
        nix = nixComponents.nix-cli;
      };
      src = fetchFromGitHub {
        owner = "NixOS";
        repo = "nix-eval-jobs";
        rev = "55e658518ae417cf26f36643fcfdebe5c5db17aa";
        hash = "sha256-4z5GnNd9cbkKChaovYghlxuh1k5rYlxNT7wpZeR1oU0=";
      };
    }
  )
