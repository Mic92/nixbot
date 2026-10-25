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
  # Same: the packages scope supplies itself; the module's copy needs no tests.
  nixbot ? null,
  callPackage,
  lib,
  stdenv,
}:
let
  nixbot-effects = callPackage ./nixbot-effects.nix { };
in
buildPythonPackage {
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

  passthru.tests = lib.optionalAttrs (nixbot != null) {
    pytest = nixbot.overridePythonAttrs {
      name = "nixbot-tests";
      doCheck = true;
      __darwinAllowLocalNetworking = true;

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
      preCheck = ''
        # Chromium refuses to start with the unwritable default HOME.
        # conftest.py gives each worker its own nix store and HOME.
        export HOME=$(mktemp -d)
        # Use internal Nix escape hatch to avoid nested sandboxing on macOS
        export _NIX_TEST_NO_SANDBOX=1
        # On huge builders more workers only add scheduling overhead and postgres connection pressure.
        export PYTEST_XDIST_AUTO_NUM_WORKERS=$(( NIX_BUILD_CORES > 64 ? 64 : NIX_BUILD_CORES ))
      '';

      # Avoid setting `PLAYWRIGHT_BROWSERS_PATH` on macOS to skip E2E tests
      env = lib.optionalAttrs stdenv.hostPlatform.isLinux {
        PLAYWRIGHT_BROWSERS_PATH = playwright-driver.browsers;
        PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
        # Chromium aborts in Skia without a fontconfig setup.
        FONTCONFIG_FILE = makeFontsConf { fontDirectories = [ dejavu_fonts ]; };
      };
    };
  };

  meta = {
    description = "A standalone CI service for Nix projects";
    homepage = "https://github.com/nix-community/nixbot";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.mic92 ];
  };
}
