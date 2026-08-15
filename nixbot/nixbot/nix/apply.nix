# nix-eval-jobs --apply function: hercules-ci build modifiers per
# derivation, delivered as `extraValue` in the job JSON.
drv: {
  buildDependenciesOnly =
    (drv.buildDependenciesOnly or false) || (drv.phases or null) == [ "noBuildPhase" ];
  ignoreFailure = drv.ignoreFailure or false;
  requireFailure = drv.requireFailure or false;
}
