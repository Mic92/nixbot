# effects-lib's `ssh`: the script it generates, run against stand-ins for
# ssh and nix-copy-closure that record their arguments. The stand-in ssh
# runs the remote command locally.
{ pkgs, ... }:
let
  effects = import ../herculesCI/effects-lib.nix { inherit pkgs; };
  fakeSsh = pkgs.writeShellScriptBin "ssh" ''
    printf '%s\n' "$@" >"$TMPDIR/ssh-args"
    exec ${pkgs.bash}/bin/bash -c "''${!#}"
  '';
  fakeNix = pkgs.writeShellScriptBin "nix-copy-closure" ''
    printf '%s\n' "$@" >"$TMPDIR/copy-args"
  '';
  remote = effects.ssh {
    destination = "root@rig";
    ssh = fakeSsh;
    nix = fakeNix;
    inheritVariables = [ "greeting" ];
  } "${pkgs.hello}/bin/hello -g \"$greeting\" >\"$TMPDIR/remote-out\"";
in
pkgs.runCommand "effects-lib-ssh" { } ''
  greeting="hi from the effect"
  ${remote}

  # The remote command's closure goes to the destination first.
  grep -qx -- --to "$TMPDIR/copy-args"
  grep -qx root@rig "$TMPDIR/copy-args"
  grep -qx ${pkgs.hello} "$TMPDIR/copy-args"
  # Then it runs there, with the inherited variable.
  grep -qx root@rig "$TMPDIR/ssh-args"
  [[ $(cat "$TMPDIR/remote-out") == "hi from the effect" ]]
  touch $out
''
