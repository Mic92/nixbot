{
  lib,
  runCommand,
  mdbook,
}:
# mdBook site built from the repo's Markdown, published by the gh-pages effect.
let
  src = ../.;

  # Title from the file's first "# " heading, so the sidebar reads
  # "GitHub Integration" instead of "GITHUB".
  titleOf =
    path:
    let
      lines = lib.splitString "\n" (builtins.readFile path);
      heading = lib.findFirst (lib.hasPrefix "# ") "# untitled" lines;
    in
    lib.removePrefix "# " heading;

  # Explicit order and grouping; readDir's alphabetical list cannot
  # express sections.
  sections = [
    {
      title = "Forge integration";
      pages = [
        "GITHUB.md"
        "GITEA.md"
        "GITLAB.md"
      ];
    }
    {
      title = "Features";
      pages = [
        "EFFECTS.md"
        "OIDC.md"
      ];
    }
    {
      title = "Operations";
      pages = [
        "LOCAL_DEVELOPMENT.md"
        "MIGRATION.md"
      ];
    }
  ];

  pageEntry = name: "- [${titleOf "${src}/docs/${name}"}](${name})";
  sectionBlock =
    s:
    ''

      # ${s.title}

    ''
    + lib.concatMapStringsSep "\n" pageEntry s.pages;

  summary = ''
    # Summary

    [Introduction](README.md)
  ''
  + lib.concatMapStringsSep "\n" sectionBlock sections;

  allPages = lib.concatMap (s: s.pages) sections;

  bookToml = ''
    [book]
    title = "nixbot"
    src = "src"

    [output.html]
    git-repository-url = "https://github.com/Mic92/nixbot"
  '';
in
runCommand "nixbot-docs"
  {
    nativeBuildInputs = [ mdbook ];
    inherit summary bookToml;
  }
  ''
    mkdir -p book/src
    printf '%s' "$bookToml" > book/book.toml
    printf '%s\n' "$summary" > book/src/SUMMARY.md
    cp ${src}/README.md book/src/README.md
    ${lib.concatMapStringsSep "\n" (name: "cp ${src}/docs/${name} book/src/${name}") allPages}
    mdbook build book -d "$out"
  ''
