# Update flake.lock on a branch and open a pull request for it: the
# counterpart of hercules-ci-effects' `flakeUpdate`, taking the same
# arguments and behaving as it does. It runs in nixbot's checkout, but
# like upstream fetches, pushes and opens the pull request with
# `tokenSecret` (the forge token by default): a forge token may only
# read, so a flake that pushes with another token names it there. Pull
# requests work on GitHub, Gitea and GitLab; auto-merge on GitHub, as
# upstream.
{
  pkgs,
  lib ? pkgs.lib,
  mkEffect,
}:
let
  openPr = pkgs.writers.writePython3Bin "nixbot-open-pr" { } (builtins.readFile ./nixbot-open-pr.py);
  # As hercules-ci-effects names it.
  genTitle =
    flakes:
    let
      names = builtins.attrNames flakes;
      showName = name: if name == "." then "`flake.lock`" else "`${name}/flake.lock`";
      sensibleNames =
        if builtins.length names > 3 then
          "`flake.lock`"
        else
          builtins.concatStringsSep ", " (map showName names);
    in
    "${sensibleNames}: Update";
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
  pullRequestTitle ? genTitle flakes,
  pullRequestBody ? null,
  # As upstream: with a branch, an existing update branch is brought up to
  # date with it by `baseMergeMethod` (merge, rebase, fast-forward or
  # reset) before updating; without one it is updated as it stands. A new
  # update branch starts from this branch, or the default branch. Pull
  # requests go to the default branch either way.
  baseMergeBranch ? null,
  baseMergeMethod ? "merge",
  flakes ? {
    "." = { inherit inputs commitSummary; };
  },
  inputs ? [ ],
  commitSummary ? "",
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
  autoMergeMethod != null -> forgeType == "github"
) "flakeUpdate: autoMergeMethod needs forgeType github, as upstream";
assert passedArgs ? flakes -> inputs == [ ] && commitSummary == "";
assert flakes != { };
let
  inherit (lib) escapeShellArg escapeShellArgs optionalString;
  tokenUser =
    if user != null then
      user
    else if forgeType == "gitlab" then
      "oauth2"
    else
      "x-access-token";
  updateFlake =
    relPath:
    {
      inputs ? [ ],
      commitSummary ? "",
    }:
    let
      where = optionalString (builtins.attrNames flakes != [ "." ]) " in '${relPath}'";
    in
    ''
      echo >&2 ${escapeShellArg "Running nix flake update${where}..."}
      (
        cd ${escapeShellArg relPath}
        # Answer "n" to accepting nixConfig, as hercules-ci-effects does.
        (yes n || :) | nix --extra-experimental-features 'nix-command flakes' \
          flake update ${escapeShellArgs inputs} --commit-lock-file \
          ${optionalString (
            commitSummary != ""
          ) "--commit-lockfile-summary ${escapeShellArg commitSummary}"}
      )
    '';
in
mkEffect {
  name = "flake-update";
  checkout = true;
  secretsMap.token = tokenSecret;
  inputs = [
    nix
    pkgs.git
  ]
  ++ lib.optional createPullRequest openPr;
  # As upstream's git-auth: git and, on GitHub, nix authenticate to the
  # forge with the token. It goes in a credential store rather than a URL,
  # so git cannot echo it.
  userSetupScript = ''
    token=$(readSecretString token .token)
    remote=${
      if gitRemote == null then
        ''$(git -C "$NIXBOT_EFFECT_CHECKOUT" config --get remote.origin.url)''
      else
        escapeShellArg gitRemote
    }
    scheme=''${remote%%://*}
    host=$(sed -E 's#^[a-z]+://([^@/]*@)?([^/]*).*#\2#' <<<"$remote")
    printf '%s://%s:%s@%s\n' "$scheme" ${escapeShellArg tokenUser} "$token" "$host" >>~/.git-credentials
    git config --global credential.helper store
  ''
  + optionalString (forgeType == "github") ''
    mkdir -p ~/.config/nix
    echo "access-tokens = $host=$token" >>~/.config/nix/nix.conf
  ''
  + ''
    unset token
  '';
  effectScript = ''
    export GIT_AUTHOR_NAME=''${GIT_AUTHOR_NAME:-nixbot}
    export GIT_AUTHOR_EMAIL=''${GIT_AUTHOR_EMAIL:-nixbot@localhost}
    export GIT_COMMITTER_NAME=$GIT_AUTHOR_NAME GIT_COMMITTER_EMAIL=$GIT_AUTHOR_EMAIL
    update_branch=${escapeShellArg updateBranch}
    base_branch=${escapeShellArg (if baseMergeBranch == null then "" else baseMergeBranch)}
    # Upstream's baseMerge.enable: a method applies only with a branch.
    method=''${base_branch:+${escapeShellArg baseMergeMethod}}

    # Fetch and push with the token from userSetupScript, not the
    # checkout's: drop the rewrites that put that one in every forge URL.
    for key in $(git config --local --name-only --get-regexp '^url\..*\.insteadof$' || :); do
      git config --unset-all "$key"
    done
    ${
      if gitRemote == null then
        ''git remote set-url origin "$(git config --get remote.origin.url | sed -E 's#://[^@/]*@#://#')"''
      else
        "git remote set-url origin ${escapeShellArg gitRemote}"
    }

    default_branch=$(git ls-remote --symref origin HEAD \
      | sed -n 's|^ref: refs/heads/\(.*\)\tHEAD$|\1|p')
    base_branch=''${base_branch:-$default_branch}
    # The checkout comes from nixbot's mirror. The forge has the branches
    # as they are now.
    git fetch --quiet origin "+refs/heads/*:refs/remotes/origin/*"
    base=refs/remotes/origin/$base_branch
    update=refs/remotes/origin/$update_branch

    die_conflict() {
      echo >&2 "Failed. Resolve the conflicts by hand and push to $update_branch:"
      git diff --name-only --diff-filter=U >&2
      exit 1
    }
    if git rev-parse --verify --quiet "$update" >/dev/null && [[ $method != reset ]]; then
      git checkout --quiet -B "$update_branch" "$update"
      case $method in
        merge) git merge --no-edit "$base" || die_conflict ;;
        rebase) git rebase "$base" || die_conflict ;;
        fast-forward)
          if ! git merge --ff-only "$base"; then
            echo >&2 "$update_branch has commits $base_branch lacks, so it cannot fast-forward."
            echo >&2 "Delete it (git push origin :$update_branch) or merge it, or use another baseMergeMethod."
            exit 1
          fi
          ;;
      esac
    else
      git checkout --quiet -B "$update_branch" "$base"
    fi

    rev_before=$(git rev-parse HEAD)
    ${lib.concatStrings (lib.mapAttrsToList updateFlake flakes)}
    if ! git diff HEAD --exit-code; then
      echo >&2 "flakeUpdate: the update left uncommitted changes"
      exit 1
    fi
    if [[ $(git rev-parse HEAD) == "$rev_before" ]]; then
      echo >&2 "No updates to push."
    else
      push_args=()
      case $method in
        rebase) push_args+=(--force-with-lease) ;;
        # Reset starts over, so whatever the branch held goes.
        reset) push_args+=(--force) ;;
      esac
      git push origin "HEAD:refs/heads/$update_branch" "''${push_args[@]}"
    fi
  ''
  + optionalString createPullRequest ''
    # Too many pull requests beats too few: make sure one is open.
    if git rev-parse --verify --quiet "$update" >/dev/null; then
      nixbot-open-pr --forge ${escapeShellArg forgeType} \
        --head "$update_branch" --base "$default_branch" \
        --title ${escapeShellArg pullRequestTitle} \
        ${optionalString (autoMergeMethod != null) "--auto-merge ${autoMergeMethod}"} \
        --body ${
          if pullRequestBody == null then
            ''"$(git log -1 --format=%b "$update")"''
          else
            escapeShellArg pullRequestBody
        }
    fi
  '';
}
