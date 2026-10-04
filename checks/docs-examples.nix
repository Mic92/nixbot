# Evaluates every ```nix block of docs/EFFECTS.md and forces the derivation
# of each effect it defines, so an example cannot rot unnoticed. Nothing is
# built. A block that no rule below knows fails the check.
{ self, pkgs, ... }:
let
  inherit (pkgs) lib;
  effectsLib = import ../herculesCI/effects-lib.nix { inherit pkgs; };

  nixosConfiguration = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    system = null; # taken from nixpkgs.hostPlatform below
    modules = [
      {
        nixpkgs.hostPlatform = "x86_64-linux";
        boot.isContainer = true;
        system.stateVersion = lib.trivial.release;
      }
    ];
  };

  # What runNixDarwin reads from a nix-darwin configuration, without the
  # nix-darwin input.
  darwinConfiguration = {
    _type = "configuration";
    _module.args.pkgs = pkgs;
    config = {
      system.build.toplevel = pkgs.runCommand "fake-darwin-system" { } "mkdir $out";
      system.profile = "/nix/var/nix/profiles/system";
      networking.hostName = "mac";
    };
  };

  # The names the examples use but do not define.
  context = {
    inherit pkgs lib;
    inherit (effectsLib) mkEffect;
    nixpkgs = self.inputs.nixpkgs;
    nixbot.lib.effects = _: effectsLib;
    self = {
      packages.x86_64-linux.image = pkgs.hello;
      nixosConfigurations.rig = nixosConfiguration;
      darwinConfigurations.mac = darwinConfiguration;
    };
    config.agenix.secrets =
      lib.genAttrs
        [
          "nix-community-effects"
          "nixbot-effects"
          "my-org-effects"
        ]
        (name: {
          path = "/run/agenix/${name}";
        });
  };

  # Every other segment of the doc split at the fences is a block.
  blocks = lib.pipe (builtins.readFile ../docs/EFFECTS.md) [
    (builtins.split "```")
    (builtins.filter builtins.isString)
    (lib.imap0 (i: text: { inherit i text; }))
    (builtins.filter ({ i, text }: lib.mod i 2 == 1 && lib.hasPrefix "nix\n" text))
    (map ({ text, ... }: lib.removePrefix "nix\n" text))
  ];

  evalBlock = text: import (builtins.toFile "doc-example.nix" "ctx: with ctx; (${text})") context;

  drvPaths =
    v:
    if v ? drvPath then
      [ v.drvPath ]
    else if lib.isAttrs v then
      lib.concatMap drvPaths (lib.attrValues v)
    else
      [ ];

  primaryRepo = tag: {
    rev = "0123456789abcdef";
    branch = if tag == null then "main" else null;
    inherit tag;
  };

  # What each kind of block yields: the effects it defines.
  rules = [
    {
      # A whole flake; its herculesCI output is what nixbot evaluates.
      name = "flake";
      matches = lib.hasPrefix "# flake.nix\n";
      effects =
        text:
        let
          flake = evalBlock text;
          outputs = flake.outputs {
            inherit (context) nixpkgs nixbot self;
          };
        in
        drvPaths (outputs.herculesCI { primaryRepo = primaryRepo null; });
    }
    {
      # A herculesCI function, tried on a branch and on a tag.
      name = "herculesCI";
      matches = lib.hasPrefix "herculesCI = ";
      effects =
        text:
        let
          fn = evalBlock (lib.removeSuffix ";" (lib.removePrefix "herculesCI = " (lib.trim text)));
        in
        drvPaths (fn {
          primaryRepo = primaryRepo null;
        })
        ++ drvPaths (fn {
          primaryRepo = primaryRepo "v1";
        });
    }
    {
      # A single attribute assignment.
      name = "assignment";
      matches = lib.hasPrefix "effects.";
      effects = text: drvPaths (evalBlock "{ ${text} }");
    }
    {
      # A NixOS module option; evaluating it must work, it has no effects.
      name = "module";
      matches = lib.hasPrefix "services.";
      effects = text: builtins.seq (builtins.deepSeq (evalBlock "{ ${text} }") true) [ ];
    }
    {
      # An expression that is an effect or a set of them.
      name = "expression";
      matches = text: lib.hasPrefix "mkEffect {" text || lib.hasPrefix "let\n" text;
      effects = text: drvPaths (evalBlock text);
    }
  ];

  checked = map (
    text:
    let
      applicable = lib.filter (r: r.matches text) rules;
      rule = lib.head applicable;
      drvs = builtins.deepSeq (rule.effects text) (rule.effects text);
    in
    assert lib.assertMsg (applicable != [ ])
      "docs/EFFECTS.md: no rule in checks/docs-examples.nix for the nix block starting\n${lib.head (lib.splitString "\n" text)}";
    assert lib.assertMsg (rule.name == "module" || drvs != [ ])
      "docs/EFFECTS.md: the ${rule.name} block starting\n${lib.head (lib.splitString "\n" text)}\ndefines no effect";
    {
      inherit (rule) name;
      inherit drvs;
    }
  ) blocks;

  seen = map (c: c.name) checked;
in
assert lib.assertMsg (blocks != [ ]) "docs/EFFECTS.md: found no nix blocks";
assert lib.assertMsg (lib.all (
  r: lib.elem r.name seen
) rules) "a rule matches no block in docs/EFFECTS.md";
pkgs.runCommand "docs-examples" { } ''
  echo ${toString (builtins.length blocks)} blocks, ${
    toString (builtins.length (lib.concatMap (c: c.drvs) checked))
  } effects evaluated
  touch $out
''
