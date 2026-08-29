# sqlc with the python codegen plugin pinned in the nix store: the
# dev shell and the freshness check (checks/sqlc.nix) regenerate with
# exactly the same plugin version, offline. sqlc looks plugins up in
# $SQLCCACHE/plugins/<sha256>/plugin.wasm before hitting the network,
# so a pre-seeded cache makes the URL in sqlc.yaml a no-op.
{ pkgs }:
let
  # Keep URL and checksum in sync with sqlc.yaml.
  pluginSha256 = "d1787aa32e61f2e73c81a4f93b3e5a9beeec918952cff3183fb96313057f58a8";
  plugin = pkgs.fetchurl {
    url = "https://github.com/rayakame/sqlc-gen-better-python/releases/download/v0.9.0/sqlc-gen-better-python.wasm";
    hash = "sha256:${pluginSha256}";
  };
in
pkgs.symlinkJoin {
  name = "sqlc-with-plugins";
  paths = [ pkgs.sqlc ];
  nativeBuildInputs = [ pkgs.makeWrapper ];
  # The cache must stay writable (sqlc keeps a wazero compilation
  # cache next to the plugins), so seed the user cache with the
  # pinned plugin instead of pointing SQLCCACHE at the store.
  postBuild = ''
    wrapProgram $out/bin/sqlc --run '
      cache="''${SQLCCACHE:-''${XDG_CACHE_HOME:-$HOME/.cache}/sqlc}"
      mkdir -p "$cache/plugins/${pluginSha256}"
      ln -sf ${plugin} "$cache/plugins/${pluginSha256}/plugin.wasm"
      export SQLCCACHE="$cache"
    '
  '';
}
