# TODO: drop when nixpkgs ships nix-eval-jobs >= 2.35.1
{ pkgs, fetchFromGitHub }:
let
  # boost.context fixes needed by nix 2.35 coroutines (NixOS/nix#16174)
  boostPatched =
    if
      pkgs.lib.versionAtLeast pkgs.boost.version "1.88" && pkgs.lib.versionOlder pkgs.boost.version "1.93"
    then
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
      })
    else
      pkgs.boost;

  # nix 2.35 eval perf regression: srcToStore cache not populated on
  # fetcher cache hits (NixOS/nix#16190, fixed after 2.35.2). Applied
  # only for nix < 2.36; patch fails if already included upstream.
  srcToStoreCachePatch = pkgs.fetchpatch {
    url = "https://github.com/NixOS/nix/commit/30820a54b112f4842bdb7df28b61b2a607e54033.patch";
    hash = "sha256-Yvn9a059LvW9FkSGH20LRPlBIhmVqQxGMBXke+hxkgs=";
  };

  patchIfNeeded =
    components:
    if pkgs.lib.versionOlder components.version "2.36" then
      components.appendPatches [ srcToStoreCachePatch ]
    else
      components;

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
  nixComponents = patchIfNeeded nixComponents_2_35;
}).overrideAttrs
  (
    finalAttrs: _prevAttrs: {
      version = "2.35.2";
      src = fetchFromGitHub {
        owner = "NixOS";
        repo = "nix-eval-jobs";
        tag = "v${finalAttrs.version}";
        hash = "sha256-qHxk1wVKqz/UMtVC14ugkhySbqYcRQbwobyeO/fhAf0=";
      };
    }
  )
