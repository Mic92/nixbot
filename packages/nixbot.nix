{
  buildPythonPackage,
  git,
  hatchling,
  nix-eval-jobs,
  pydantic,
  pytestCheckHook,
  pytest-asyncio,
  pytest-timeout,
  pytest-xdist,
  pytest-benchmark,
  fastapi,
  uvicorn,
  asyncpg,
  jinja2,
  httpx,
  joserfc,
  zstandard,
  python-multipart,
  postgresql,
  playwright,
  playwright-driver,
  pyte,
  makeFontsConf,
  dejavu_fonts,
  # Optional: the NixOS module calls this file with python.pkgs.callPackage,
  # which cannot supply it. Only the pytest passthru check needs the CLI.
  nixbot-cli ? null,
  callPackage,
  lib,
}:
let
  nixbot-effects = callPackage ./nixbot-effects.nix { };
in
buildPythonPackage (finalAttrs: {
  name = "nixbot";
  pyproject = true;
  src = ./../nixbot;
  build-system = [ hatchling ];
  dependencies = [
    pydantic
    fastapi
    uvicorn
    asyncpg
    jinja2
    httpx
    joserfc
    zstandard
    python-multipart
    nixbot-effects
  ];

  buildInputs = [ nix-eval-jobs.nix ];

  # Tests run in passthru.tests.pytest to keep the test closure
  # (playwright browsers, postgresql) out of the package build.
  doCheck = false;

  passthru.tests.pytest = finalAttrs.finalPackage.overrideAttrs {
    name = "nixbot-tests";
    doCheck = true;
  };

  nativeCheckInputs = [
    git
    # For the eval/prefetch integration tests: nix works daemon-less
    # against a scratch store set up in preCheck.
    nix-eval-jobs.nix
    nix-eval-jobs
    pytestCheckHook
    pytest-asyncio
    pytest-timeout
    pytest-xdist
    pytest-benchmark
    postgresql
    playwright
    pyte
  ]
  # The CLI's tests (test_cli*.py) run against this app in-process.
  ++ lib.optional (nixbot-cli != null) nixbot-cli;

  # Browser tests run headless Chromium from the pinned playwright
  # driver; the version matches the python playwright package.
  # Chromium refuses to start with the unwritable default HOME.
  preCheck = ''
    export HOME=$(mktemp -d)
    # Daemon-less scratch nix store for the eval/prefetch integration
    # tests; the real /nix/store is read-only inside the build sandbox.
    export NIX_STORE_DIR=$TMPDIR/nix/store
    export NIX_STATE_DIR=$TMPDIR/nix/var
    export NIX_CONF_DIR=$TMPDIR/nix/etc
    # On huge builders more workers only add scheduling overhead and postgres connection pressure.
    export PYTEST_XDIST_AUTO_NUM_WORKERS=$(( NIX_BUILD_CORES > 64 ? 64 : NIX_BUILD_CORES ))
  '';

  env = {
    PLAYWRIGHT_BROWSERS_PATH = playwright-driver.browsers;
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
    # Chromium aborts in Skia without a fontconfig setup.
    FONTCONFIG_FILE = makeFontsConf { fontDirectories = [ dejavu_fonts ]; };
  };

  meta = {
    description = "A standalone CI service for Nix projects";
    homepage = "https://github.com/nix-community/nixbot";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.mic92 ];
  };
})
