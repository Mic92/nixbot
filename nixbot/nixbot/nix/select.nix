# nix-eval-jobs --select function: build the configured attribute
# (default `checks`) as job `default.<attribute>.`. Effects are
# discovered separately by nixbot_effects.
{
  # The configured attribute as a path, e.g. [ "hydraJobs" "ci" ].
  attrPath,
  # Instance-configured systems; null builds everything.
  evalSystems,
  # Historic "default" job prefix for compat; null omits it.
  jobPrefix,
}:
flake:
let
  perSystemOutputs = [
    "checks"
    "packages"
    "devShells"
    "formatter"
  ];
  isDrv = v: (v.type or null) == "derivation";
  # Lazy in the attr values so a throwing attribute is reported at its
  # own path instead of failing the whole set.
  prune =
    v:
    if !builtins.isAttrs v || isDrv v then
      v
    else if v ? _type || !(v.recurseForDerivations or true) then
      { }
    else
      builtins.mapAttrs (n: prune) v;
  getAt =
    set: path: builtins.foldl' (s: n: if builtins.isAttrs s && s ? ${n} then s.${n} else { }) set path;
  setAt =
    path: v: if path == [ ] then v else { ${builtins.head path} = setAt (builtins.tail path) v; };
  configured0 = getAt flake.outputs attrPath;
  # Only schema outputs have systems at the top level; hydraJobs etc.
  # have arbitrary names there and must not be filtered.
  configured =
    if
      evalSystems != null
      && builtins.isAttrs configured0
      && builtins.length attrPath == 1
      && builtins.elem (builtins.head attrPath) perSystemOutputs
    then
      builtins.intersectAttrs (builtins.listToAttrs (
        map (s: {
          name = s;
          value = { };
        }) evalSystems
      )) configured0
    else
      configured0;
  job = setAt attrPath (prune configured);
in
if jobPrefix == null then job else { ${jobPrefix} = job; }
