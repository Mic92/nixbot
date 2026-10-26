# Update flake.lock on a branch and open a pull request for it: the
# counterpart of hercules-ci-effects' `flakeUpdate`, taking the same
# arguments and behaving as it does. It runs in nixbot's checkout, but
# like upstream fetches, pushes and opens the pull request with
# `tokenSecret` (the forge token by default): a forge token may only
# read, so a flake that pushes with another token names it there. Pull
# requests work on GitHub, Gitea and GitLab; auto-merge on GitHub and
# Gitea. The work is done by nixbot-flake-update.py.
{
  pkgs,
  lib ? pkgs.lib,
  mkEffect,
}:
let
  script = pkgs.writers.writePython3Bin "nixbot-flake-update" {
    # ruff format owns the layout.
    flakeIgnore = [
      "E203"
      "E501"
      "W503"
    ];
  } (builtins.readFile ./nixbot-flake-update.py);
in
passedArgs@{
  # The checkout's origin if null.
  gitRemote ? null,
  # The user the token authenticates as; the forge's token user if null.
  user ? null,
  tokenSecret ? {
    type = "GitToken";
  },
  updateBranch ? "flake-update",
  forgeType ? "github",
  createPullRequest ? true,
  autoMergeMethod ? null,
  # Upstream's: "`flake.lock`: Update", naming the flakes it updates.
  pullRequestTitle ? null,
  pullRequestBody ? null,
  # As upstream: with a branch, an existing update branch is brought up to
  # date with it by `baseMergeMethod` (merge, rebase, fast-forward or
  # reset) before updating; without one it is updated as it stands. A new
  # update branch starts from this branch, or the default branch. Pull
  # requests go to the default branch either way.
  baseMergeBranch ? null,
  baseMergeMethod ? "merge",
  flakes ? {
    "." = { inherit inputs excludeInputs commitSummary; };
  },
  inputs ? [ ],
  # Not upstream: inputs of the lock file to leave alone.
  excludeInputs ? [ ],
  commitSummary ? "",
  # Not upstream: a branch `<updateBranch>-<input>` and pull request per
  # input (of `inputs`, or all), each started afresh from the base.
  perInput ? false,
  # Not upstream, where `module` extends `git.update.script`: bash run in
  # the checkout's root after the lock files are updated, before the
  # push, for files that have to change with them (a vendor hash, a
  # generated deps.nix). It commits what it changes; uncommitted changes
  # fail the effect. With perInput it runs once per input.
  postUpdate ? null,
  nix ? pkgs.nix,
}:
assert lib.assertOneOf "forgeType" forgeType [
  "github"
  "gitea"
  "gitlab"
];
assert lib.assertOneOf "baseMergeMethod" baseMergeMethod [
  "merge"
  "rebase"
  "fast-forward"
  "reset"
];
assert lib.assertOneOf "autoMergeMethod" autoMergeMethod [
  null
  "merge"
  "rebase"
  "squash"
];
assert lib.assertMsg (
  autoMergeMethod != null -> forgeType != "gitlab"
) "flakeUpdate: autoMergeMethod needs forgeType github or gitea";
assert passedArgs ? flakes -> inputs == [ ] && excludeInputs == [ ] && commitSummary == "";
assert flakes != { };
let
  config = pkgs.writeText "flake-update.json" (
    builtins.toJSON {
      inherit
        gitRemote
        user
        updateBranch
        forgeType
        createPullRequest
        autoMergeMethod
        pullRequestTitle
        pullRequestBody
        baseMergeBranch
        baseMergeMethod
        flakes
        perInput
        ;
      nix = lib.getExe nix;
      postUpdate =
        if postUpdate == null then
          null
        else
          pkgs.writeShellScript "flake-update-post-update" ''
            set -euo pipefail
            ${postUpdate}
          '';
    }
  );
in
mkEffect {
  name = "flake-update";
  checkout = true;
  secretsMap.token = tokenSecret;
  inputs = [ pkgs.git ];
  effectScript = "${lib.getExe script} ${config}";
}
