{
  config,
  options,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.services.nixbot;
  inherit (config.services.nixbot) packages;

  cleanUpRepoName =
    name: builtins.replaceStrings [ "/" ":" "*" ] [ "_slash_" "_colon_" "_star_" ] name;

  webUnixSocket = "/run/nixbot/web.sock";

  hasSSL =
    if cfg.nginx.enable then
      let
        host = config.services.nginx.virtualHosts.${cfg.domain};
      in
      host.forceSSL || host.addSSL || cfg.useHTTPS
    else
      cfg.useHTTPS;

  # Without the managed nginx vhost (or an external TLS proxy implied by
  # useHTTPS), the service only listens on cfg.port.
  baseUrl = "${if hasSSL then "https" else "http"}://${cfg.domain}${
    lib.optionalString (!cfg.nginx.enable && !cfg.useHTTPS) ":${toString cfg.port}"
  }/";

  localDbUrl = "postgresql://nixbot@/nixbot?host=/run/postgresql";

  serviceConfig = (pkgs.formats.json { }).generate "nixbot-config.json" (
    {
      build_systems = cfg.buildSystems;
      eval_systems = cfg.evalSystems;
      url = baseUrl;
      webhook_base_url = cfg.webhookBaseUrl;
      state_dir = "/var/lib/nixbot";
      admins = cfg.admins;
      private_repo_viewers = cfg.privateRepoViewers;
      eval_max_memory_size = cfg.evalMaxMemorySize;
      eval_worker_count = cfg.evalWorkerCount;
      build_concurrency = cfg.buildConcurrency;
      gitea =
        if !cfg.gitea.enable then
          null
        else
          {
            instance_url = cfg.gitea.instanceUrl;
            filters = {
              user_allowlist = cfg.gitea.userAllowlist;
              repo_allowlist = cfg.gitea.repoAllowlist;
              topic = cfg.gitea.topic;
            };
            token_file = "gitea-token";
            oauth_id = cfg.gitea.oauthId;
            oauth_secret_file = if cfg.gitea.oauthSecretFile != null then "gitea-oauth-secret" else null;
            ssh_private_key_file = if cfg.gitea.sshPrivateKeyFile != null then "gitea-ssh-key" else null;
            ssh_known_hosts_file = cfg.gitea.sshKnownHostsFile;
          };
      gitlab =
        if !cfg.gitlab.enable then
          null
        else
          {
            instance_url = cfg.gitlab.instanceUrl;
            filters = {
              user_allowlist = cfg.gitlab.userAllowlist;
              repo_allowlist = cfg.gitlab.repoAllowlist;
              topic = cfg.gitlab.topic;
            };
            token_file = "gitlab-token";
            oauth_id = cfg.gitlab.oauthId;
            oauth_secret_file = if cfg.gitlab.oauthSecretFile != null then "gitlab-oauth-secret" else null;
            ssh_private_key_file = if cfg.gitlab.sshPrivateKeyFile != null then "gitlab-ssh-key" else null;
            ssh_known_hosts_file = cfg.gitlab.sshKnownHostsFile;
          };
      github =
        if !cfg.github.enable then
          null
        else
          {
            id = cfg.github.appId;
            api_url = cfg.github.apiUrl;
            secret_key_file = "github-app-secret-key";
            webhook_secret_file = "github-webhook-secret";
            filters = {
              user_allowlist = cfg.github.userAllowlist;
              repo_allowlist = cfg.github.repoAllowlist;
              topic = cfg.github.topic;
            };
            oauth_id = cfg.github.oauthId;
            oauth_secret_file = if cfg.github.oauthSecretFile != null then "github-oauth-secret" else null;
          };
      pull_based =
        if cfg.pullBased.repositories == { } then
          null
        else
          {
            repositories = lib.flip lib.mapAttrs cfg.pullBased.repositories (
              name: repo: {
                inherit name;
                default_branch = repo.defaultBranch;
                url = repo.url;
                poll_interval = repo.pollInterval;
                ssh_private_key_file =
                  if repo.sshPrivateKeyFile != null then "pull-based__${cleanUpRepoName name}" else null;
                ssh_known_hosts_file = repo.sshKnownHostsFile;
              }
            );
            poll_spread = cfg.pullBased.pollSpread;
          };
      oidc =
        if !cfg.oidc.enable then
          null
        else
          {
            name = cfg.oidc.name;
            discovery_url = cfg.oidc.discoveryUrl;
            client_id = cfg.oidc.clientId;
            scope = cfg.oidc.scope;
            mapping = cfg.oidc.mapping;
            client_secret_file = "oidc-client-secret";
          };
      workload_identity = {
        enable = cfg.workloadIdentity.enable;
        signing_key_file =
          if cfg.workloadIdentity.signingKeyFile != null then "workload-identity-key" else null;
        token_ttl = cfg.workloadIdentity.tokenTtl;
        key_rotation_days = cfg.workloadIdentity.keyRotationDays;
      };
      outputs_path = cfg.outputsPath;
      post_build_steps = map (step: {
        name = step.name;
        environment = step.environment;
        command = step.command;
        warn_only = step.warnOnly;
      }) cfg.postBuildSteps;
      failed_build_report_limit = cfg.failedBuildReportLimit;
      status_context_prefix = cfg.statusContextPrefix;
      branches = cfg.branches;
      gcroots_dir = "/nix/var/nix/gcroots/per-user/nixbot";
      effects_per_repo_secrets = lib.mapAttrs' (name: _path: {
        inherit name;
        value = "effects-secret__${cleanUpRepoName name}";
      }) cfg.effects.perRepoSecretFiles;
      effects_extra_sandbox_paths = cfg.effects.extraSandboxPaths;
      effects_mountables_file =
        if cfg.effects.mountables == { } then
          null
        else
          pkgs.writeText "effects-mountables.json" (builtins.toJSON cfg.effects.mountables);
      effects_extra_nix_options = cfg.effects.extraNixOptions;
      show_trace_on_failure = cfg.showTrace;
      cache_failed_builds = cfg.cacheFailedBuilds;
      allow_unauthenticated_control = cfg.allowUnauthenticatedControl;
      proxy_auth_header = cfg.proxyAuthHeader;
      build_max_silent_time = cfg.buildMaxSilentTime;
      build_timeout = cfg.buildTimeout;
      http_port = cfg.port;
      http_unix_socket = if cfg.nginx.enable then webUnixSocket else null;
    }
    // (
      if cfg.database.createLocally then
        { db_url = localDbUrl; }
      else if cfg.database.urlFile != null then
        { db_url_file = "db-url"; }
      else
        { db_url = cfg.database.url; }
    )
  );
in
{
  imports = [
    ./packages.nix
    ./cachix.nix
    ./niks3.nix
    (lib.mkRemovedOptionModule [ "services" "nixbot" "github" "oauthPrivateRepoScope" ] ''
      Private-repo visibility is now determined with nixbot's own forge
      credentials instead of the user's login token, so the write-capable
      "repo" OAuth scope is no longer requested.
    '')
  ];

  options.services.nixbot = {
    enable = lib.mkEnableOption "the nixbot CI service";

    domain = lib.mkOption {
      type = lib.types.str;
      description = "Domain under which the web frontend is reachable.";
      example = "ci.example.com";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8010;
      description = "TCP port the service listens on.";
    };

    webhookBaseUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "URL base for registered webhooks when it differs from the frontend URL.";
      example = "https://ci-webhooks.example.com/";
    };

    useHTTPS = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Force https:// URLs when running behind a reverse proxy other than the
        nginx virtual host managed by this module.
      '';
    };

    admins = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = ''
        Users allowed to trigger builds and change settings.
        Entries must be provider-qualified, e.g. "github:alice",
        "gitea:bob" or "oidc:<issuer>:carol"; plain usernames never match.
      '';
      example = [ "github:alice" ];
    };

    privateRepoViewers = lib.mkOption {
      type = lib.types.attrsOf (lib.types.listOf lib.types.str);
      default = { };
      description = ''
        Visibility of private repositories for logged-in users without
        forge access (e.g. OIDC logins). Keys select repositories
        ("forge:owner/repo", "forge:owner/*" or "*"; the most specific
        key wins). Values are viewer rules: a provider-qualified
        identity, "provider:*" for any authenticated user of that
        provider, or "provider:group:<name>" matching the OIDC groups
        claim (set oidc.mapping.groups and include the groups scope).
      '';
      example = {
        "*" = [ "oidc:auth.example.com:group:ci" ];
        "gitlab:acme/*" = [ "oidc:auth.example.com:*" ];
      };
    };

    buildSystems = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ pkgs.stdenv.hostPlatform.system ];
      defaultText = lib.literalExpression "[ pkgs.stdenv.hostPlatform.system ]";
      description = "Systems to build (others come via nix remote builders).";
    };

    evalSystems = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Systems to evaluate; an empty list evaluates every system exposed by the flake.";
    };

    buildConcurrency = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = null;
      description = "Global cap on concurrent attribute builds. Defaults to the CPU count.";
    };

    evalMaxMemorySize = lib.mkOption {
      type = lib.types.int;
      default = 2048;
      description = "Maximum memory size for nix-eval-jobs (in MiB) per worker.";
    };

    evalWorkerCount = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = null;
      description = "Number of nix-eval-jobs worker processes; null uses the core count.";
    };

    showTrace = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Show stack traces on failed evaluations.";
    };

    buildMaxSilentTime = lib.mkOption {
      type = lib.types.int;
      default = 60 * 20;
      description = "Maximum time in seconds a nix build can be silent before being killed.";
    };

    buildTimeout = lib.mkOption {
      type = lib.types.int;
      default = 60 * 60 * 3;
      description = "Overall timeout in seconds for nix builds.";
    };

    cacheFailedBuilds = lib.mkEnableOption "caching failed builds, skipping them until explicitly rebuilt";

    allowUnauthenticatedControl = lib.mkEnableOption ''
      unauthenticated control actions (cancel, restart). Useful behind a VPN
      where network access implies trust
    '';

    proxyAuthHeader = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        HTTP header carrying the authenticated username, set by the reverse
        proxy. Users are qualified as `proxy:<username>` (e.g. in `admins`).
        The proxy MUST set or strip this header on every request, otherwise
        clients can impersonate any user.
      '';
      example = "X-Remote-User";
    };

    statusContextPrefix = lib.mkOption {
      type = lib.types.str;
      default = "nixbot";
      example = "buildbot";
      description = ''
        Prefix of check-run names / commit-status contexts
        ("<prefix>/nix-eval", "<prefix>/nix-build"). Set to "buildbot"
        when migrating from buildbot-nix to keep existing branch
        protection rules working; changing the prefix requires updating
        required status checks on every repository.
      '';
    };

    failedBuildReportLimit = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 47;
      description = ''
        Maximum number of failed builds reported as individual check
        runs / commit statuses per evaluation (3 of the typical 50
        slots stay reserved for the eval/build/effects summaries).
      '';
    };

    outputsPath = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/var/www/nixbot/nix-outputs/";
      description = ''
        Path where the latest output store paths per attribute are stored as
        text files, exposed via nginx at ''${domain}/nix-outputs.
      '';
    };

    database = {
      createLocally = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Provision a local PostgreSQL database, connected over the unix
          socket with peer authentication.
        '';
      };

      url = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "postgresql://nixbot@db.example.com/nixbot";
        description = "Connection URL for a remote database without secrets.";
      };

      urlFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = ''
          File containing the connection URL for a remote database; use this
          when the URL carries a password.
        '';
      };
    };

    github = {
      enable = lib.mkEnableOption "GitHub integration";

      appId = lib.mkOption {
        type = lib.types.int;
        description = "GitHub App ID.";
      };

      apiUrl = lib.mkOption {
        type = lib.types.str;
        default = "https://api.github.com";
        description = "GitHub API base URL (override for GitHub Enterprise).";
      };

      appSecretKeyFile = lib.mkOption {
        type = lib.types.path;
        description = "GitHub App private key file.";
      };

      webhookSecretFile = lib.mkOption {
        type = lib.types.path;
        description = "GitHub webhook secret file.";
      };

      oauthId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "GitHub OAuth client id, used for the login button.";
      };

      oauthSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "GitHub OAuth client secret file.";
      };

      userAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only repositories owned by these users/organizations are built.";
      };

      repoAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only these repositories are built.";
      };

      topic = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "build-with-buildbot";
        description = ''
          Legacy import aid: on the first startup with an empty database,
          repositories carrying this topic are enabled automatically.
          Afterwards the topic is ignored; enable or disable projects in
          the web UI.
        '';
      };
    };

    gitea = {
      enable = lib.mkEnableOption "Gitea integration";

      instanceUrl = lib.mkOption {
        type = lib.types.str;
        description = "Gitea instance URL.";
      };

      tokenFile = lib.mkOption {
        type = lib.types.path;
        description = "Gitea API token file.";
      };

      oauthId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Gitea OAuth client id, used for the login button.";
      };

      oauthSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Gitea OAuth client secret file.";
      };

      userAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only repositories owned by these users/organizations are built.";
      };

      repoAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only these repositories are built.";
      };

      topic = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "build-with-buildbot";
        description = ''
          Legacy import aid: on the first startup with an empty database,
          repositories carrying this topic are enabled automatically.
          Afterwards the topic is ignored; enable or disable projects in
          the web UI.
        '';
      };

      sshPrivateKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "SSH key used to fetch repositories, if non-null.";
      };

      sshKnownHostsFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "known_hosts file matched when fetching over SSH.";
      };
    };

    gitlab = {
      enable = lib.mkEnableOption "GitLab integration";

      instanceUrl = lib.mkOption {
        type = lib.types.str;
        default = "https://gitlab.com";
        description = "GitLab instance URL.";
      };

      tokenFile = lib.mkOption {
        type = lib.types.path;
        description = "GitLab access token file (api scope).";
      };

      oauthId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "GitLab OAuth application id, used for the login button.";
      };

      oauthSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "GitLab OAuth application secret file.";
      };

      userAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only repositories owned by these users/groups are built.";
      };

      repoAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only these repositories are built.";
      };

      topic = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "build-with-buildbot";
        description = ''
          Legacy import aid: on the first startup with an empty database,
          repositories carrying this topic are enabled automatically.
          Afterwards the topic is ignored; enable or disable projects in
          the web UI.
        '';
      };

      sshPrivateKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "SSH key used to fetch repositories, if non-null.";
      };

      sshKnownHostsFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "known_hosts file matched when fetching over SSH.";
      };
    };

    oidc = {
      enable = lib.mkEnableOption "OIDC login";

      name = lib.mkOption {
        type = lib.types.str;
        default = "OIDC Provider";
        description = "User facing name of this provider.";
      };

      discoveryUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "https://id.example.com/.well-known/openid-configuration";
        description = "OIDC discovery endpoint URL.";
      };

      clientId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "OIDC client ID.";
      };

      clientSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "File containing the OIDC client secret.";
      };

      scope = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [
          "openid"
          "email"
          "profile"
        ];
        description = "Requested OIDC scopes.";
      };

      mapping = lib.mkOption {
        description = ''
          How OIDC claims map to user info. The username claim forms the
          identity used for admin/viewer matching
          ("oidc:<issuer-host>:<claim>"); it defaults to the stable "sub"
          because a user-editable claim such as preferred_username would
          allow hijacking someone else's admin entry.
        '';
        default = {
          username = "sub";
          groups = null;
        };
        type = lib.types.submodule {
          options = {
            username = lib.mkOption {
              type = lib.types.str;
              default = "sub";
            };
            groups = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
            };
          };
        };
      };
    };

    workloadIdentity = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Serve OIDC discovery/JWKS documents and mint short-lived ID
          tokens for effects that declare idTokenAudiences, so they can
          authenticate to relying parties (Vault, AWS, niks3, ...)
          without static secrets. See docs/WORKLOAD_IDENTITY.md.
        '';
      };

      signingKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = ''
          RSA private key (PEM) used to sign ID tokens. When unset, a
          key is generated in the state directory and rotated
          automatically.
        '';
      };

      tokenTtl = lib.mkOption {
        type = lib.types.ints.positive;
        default = 300;
        description = "Lifetime of issued ID tokens in seconds.";
      };

      keyRotationDays = lib.mkOption {
        type = lib.types.ints.positive;
        default = 30;
        description = "Rotation interval of the auto-generated signing key.";
      };
    };

    pullBased = {
      repositories = lib.mkOption {
        default = { };
        description = "Repositories to poll for changes.";
        type = lib.types.attrsOf (
          lib.types.submodule {
            options = {
              defaultBranch = lib.mkOption {
                type = lib.types.str;
                description = "The repository's default branch.";
              };

              url = lib.mkOption {
                type = lib.types.str;
                description = "The repository's URL, must be fetchable by git.";
              };

              pollInterval = lib.mkOption {
                type = lib.types.addCheck lib.types.int (x: x > 0);
                default = cfg.pullBased.pollInterval;
                description = "How often to poll this repository, in seconds.";
              };

              sshPrivateKeyFile = lib.mkOption {
                type = lib.types.nullOr lib.types.path;
                default = cfg.pullBased.sshPrivateKeyFile;
                description = "SSH key used to fetch this repository.";
              };

              sshKnownHostsFile = lib.mkOption {
                type = lib.types.nullOr lib.types.path;
                default = cfg.pullBased.sshKnownHostsFile;
                description = "known_hosts file matched when fetching over SSH.";
              };
            };
          }
        );
      };

      pollInterval = lib.mkOption {
        type = lib.types.addCheck lib.types.int (x: x > 0);
        default = 60;
        description = "Default poll interval in seconds.";
      };

      pollSpread = lib.mkOption {
        type = lib.types.nullOr lib.types.int;
        default = null;
        description = "Randomly spread polls apart up to this many seconds.";
      };

      sshPrivateKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Default SSH key used to fetch repositories.";
      };

      sshKnownHostsFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Default known_hosts file matched when fetching over SSH.";
      };
    };

    postBuildSteps = lib.mkOption {
      default = [ ];
      description = "Steps to execute after every successful build.";
      type = lib.types.listOf (
        lib.types.submodule {
          options = {
            name = lib.mkOption {
              type = lib.types.str;
              description = "Name of the post-build step, shown in the UI.";
            };

            environment = lib.mkOption {
              type = lib.types.attrsOf lib.types.anything;
              default = { };
              description = ''
                Extra environment variables for this step. Use
                `inputs.nixbot.lib.interpolate "%(prop:out_link)s"`
                for per-build placeholders. Available properties: attr,
                out_link (the build's result symlink), out_path, drv_path,
                system, project, branch, revision, pr_number,
                default_branch. `%(secret:NAME)s` reads a
                systemd credential; load it via
                `systemd.services.nixbot.serviceConfig.LoadCredential`.
              '';
            };

            command = lib.mkOption {
              type = lib.types.listOf lib.types.anything;
              description = ''
                Command to execute, passed to execve verbatim (no shell).
              '';
            };

            warnOnly = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Failures only warn instead of failing the build.";
            };
          };
        }
      );
    };

    effects = {
      perRepoSecretFiles = lib.mkOption {
        type = lib.types.attrsOf lib.types.path;
        default = { };
        description = ''
          Per-repository or per-organization JSON secrets files for effects.
          Keys: "github:org/*", "gitea:org/*" or exact "github:owner/repo".
        '';
      };

      extraSandboxPaths = lib.mkOption {
        type = lib.types.listOf lib.types.path;
        default = [ ];
        description = "Extra host paths bind-mounted read-only into the effects sandbox.";
      };

      mountables = lib.mkOption {
        type = lib.types.attrsOf (
          lib.types.submodule {
            options = {
              source = lib.mkOption {
                type = lib.types.path;
                description = "Host path to mount.";
              };
              readOnly = lib.mkOption {
                type = lib.types.bool;
                default = true;
              };
              condition = lib.mkOption {
                type = lib.types.anything;
                description = ''
                  Hercules secret condition controlling which effect
                  invocations may request this mountable, e.g.
                  { isOwner = "my-org"; } or "isDefaultBranch".
                '';
              };
            };
          }
        );
        default = { };
        description = ''
          Named host paths effects may request via __hci_effect_mounts
          (hercules-ci mountables).
        '';
      };

      extraNixOptions = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { };
        description = "nix options for the effect sandbox's private daemon.";
      };
    };

    branches = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            matchGlob = lib.mkOption {
              type = lib.types.str;
              description = "Glob selecting which branches this rule applies to.";
            };

            registerGCRoots = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Register gcroots for matching branches.";
            };

            updateOutputs = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Update the outputs directory for matching branches.";
            };
          };
        }
      );
      default = { };
      description = ''
        Branch rules; matching rules are or-ed together. Default branches
        always behave as if all options were true.
      '';
    };

    nginx = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Manage an nginx virtual host proxying to the service.";
      };

      enableACME = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Request an ACME certificate and force SSL on the virtual host.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    # A configured but disabled forge is more likely a migration
    # accident than intent.
    warnings =
      lib.optional (!cfg.github.enable && options.services.nixbot.github.appId.isDefined)
        "nixbot: github.* is configured but github.enable is false; GitHub projects will not appear. Set services.nixbot.github.enable = true."
      ++
        lib.optional (!cfg.gitea.enable && options.services.nixbot.gitea.tokenFile.isDefined)
          "nixbot: gitea.* is configured but gitea.enable is false; Gitea projects will not appear. Set services.nixbot.gitea.enable = true."
      ++
        lib.optional (!cfg.gitlab.enable && options.services.nixbot.gitlab.tokenFile.isDefined)
          "nixbot: gitlab.* is configured but gitlab.enable is false; GitLab projects will not appear. Set services.nixbot.gitlab.enable = true.";

    assertions = [
      {
        assertion = (cfg.github.oauthId != null) == (cfg.github.oauthSecretFile != null);
        message = "github.oauthId and github.oauthSecretFile must be set together.";
      }
      {
        assertion = (cfg.gitea.oauthId != null) == (cfg.gitea.oauthSecretFile != null);
        message = "gitea.oauthId and gitea.oauthSecretFile must be set together.";
      }
      {
        assertion = (cfg.gitlab.oauthId != null) == (cfg.gitlab.oauthSecretFile != null);
        message = "gitlab.oauthId and gitlab.oauthSecretFile must be set together.";
      }
      {
        assertion =
          cfg.oidc.enable
          -> (
            cfg.oidc.discoveryUrl != null && cfg.oidc.clientId != null && cfg.oidc.clientSecretFile != null
          );
        message = "oidc.enable requires oidc.discoveryUrl, oidc.clientId and oidc.clientSecretFile.";
      }
      {
        assertion = cfg.database.createLocally || cfg.database.url != null || cfg.database.urlFile != null;
        message = "Set database.url or database.urlFile when database.createLocally is disabled.";
      }
      {
        assertion =
          cfg.database.createLocally -> (cfg.database.url == null && cfg.database.urlFile == null);
        message = "database.url/database.urlFile are ignored while database.createLocally is enabled; disable it to use a remote database.";
      }
    ];

    users.users.nixbot = {
      isSystemUser = true;
      group = "nixbot";
      home = "/var/lib/nixbot";
    };
    users.groups.nixbot = { };
    # The web socket is group-restricted (0660).
    users.users.nginx = lib.mkIf cfg.nginx.enable { extraGroups = [ "nixbot" ]; };

    nix.settings.extra-allowed-users = [ "nixbot" ];

    # Socket activation: the listener outlives service restarts, so
    # nginx (or webhook deliveries on the TCP port) queue during a
    # redeploy instead of getting connection-refused.
    systemd.sockets.nixbot = {
      description = "nixbot CI service socket";
      wantedBy = [ "sockets.target" ];
      socketConfig = {
        ListenStream = if cfg.nginx.enable then webUnixSocket else cfg.port;
        # nginx connects as group member.
        SocketUser = "nixbot";
        SocketGroup = "nixbot";
        SocketMode = "0660";
        # The socket path lives here; owned by the socket unit so a
        # service stop does not remove it.
        RuntimeDirectory = "nixbot";
      };
    };

    systemd.services.nixbot = {
      description = "nixbot CI service";
      wantedBy = [ "multi-user.target" ];
      after = [
        "network-online.target"
        "nixbot.socket"
      ]
      ++ lib.optional cfg.database.createLocally "postgresql.target";
      wants = [ "network-online.target" ];
      requires = [
        "nixbot.socket"
      ]
      ++ lib.optional cfg.database.createLocally "postgresql.target";

      path = [
        pkgs.git
        pkgs.openssh
        pkgs.openssl
        pkgs.bash
        pkgs.coreutils
        pkgs.bubblewrap
        packages.nix-eval-jobs
        config.nix.package
      ];

      environment = {
        # Remote builders need a HOME for ~/.ssh.
        HOME = "/var/lib/nixbot";
      };

      serviceConfig = {
        ExecStart = "${lib.getExe' packages.nixbot "nixbot"} --config ${serviceConfig}";
        # Fail activation if the service never becomes healthy.
        # With nginx the engine only listens on the unix socket; probe
        # whatever listener is actually configured.
        ExecStartPost = pkgs.writeShellScript "nixbot-health" ''
          for _ in $(seq 60); do
            if ${pkgs.curl}/bin/curl -fsS --max-time 5 \
              ${
                if cfg.nginx.enable then
                  ''--unix-socket ${webUnixSocket} "http://localhost/health"''
                else
                  ''"http://127.0.0.1:${toString cfg.port}/health"''
              } >/dev/null; then
              exit 0
            fi
            sleep 1
          done
          echo "nixbot did not become healthy" >&2
          exit 1
        '';
        # The health loop above can take up to ~360s (60 iterations of
        # 5s curl timeout + 1s sleep), e.g. during long migrations;
        # systemd's default TimeoutStartSec=90s would kill the startup
        # mid-migration.
        TimeoutStartSec = 600;
        User = "nixbot";
        Group = "nixbot";
        StateDirectory = "nixbot";
        # Private repository clones and build logs live here.
        StateDirectoryMode = "0700";
        RuntimeDirectory = "nixbot";
        # The socket unit shares /run/nixbot; removing it on service
        # stop would yank the listening socket's path.
        RuntimeDirectoryPreserve = true;
        WorkingDirectory = "/var/lib/nixbot";
        Restart = "on-failure";
        RestartSec = 5;
        # Signal only the main process on stop. The default
        # control-group mode also SIGTERMs in-flight `nix build`
        # children, which exit non-zero and get recorded as build
        # failures before nixbot can cancel them; recovery then sees a
        # terminal build and never resumes it. With mixed, nixbot cancels
        # the build tasks and reaps the children itself, leaving the
        # attributes "building" for recovery.
        KillMode = "mixed";
        # Headroom for nixbot to cancel builds and reap nix children
        # before systemd SIGKILLs the cgroup.
        TimeoutStopSec = 180;

        LoadCredential =
          lib.optionals cfg.github.enable [
            "github-app-secret-key:${cfg.github.appSecretKeyFile}"
            "github-webhook-secret:${cfg.github.webhookSecretFile}"
          ]
          ++ lib.optional (
            cfg.github.enable && cfg.github.oauthSecretFile != null
          ) "github-oauth-secret:${cfg.github.oauthSecretFile}"
          ++ lib.optional cfg.gitea.enable "gitea-token:${cfg.gitea.tokenFile}"
          ++ lib.optional (
            cfg.gitea.enable && cfg.gitea.oauthSecretFile != null
          ) "gitea-oauth-secret:${cfg.gitea.oauthSecretFile}"
          ++ lib.optional (
            cfg.gitea.enable && cfg.gitea.sshPrivateKeyFile != null
          ) "gitea-ssh-key:${cfg.gitea.sshPrivateKeyFile}"
          ++ lib.optional cfg.gitlab.enable "gitlab-token:${cfg.gitlab.tokenFile}"
          ++ lib.optional (
            cfg.gitlab.enable && cfg.gitlab.oauthSecretFile != null
          ) "gitlab-oauth-secret:${cfg.gitlab.oauthSecretFile}"
          ++ lib.optional (
            cfg.gitlab.enable && cfg.gitlab.sshPrivateKeyFile != null
          ) "gitlab-ssh-key:${cfg.gitlab.sshPrivateKeyFile}"
          ++ lib.optional cfg.oidc.enable "oidc-client-secret:${cfg.oidc.clientSecretFile}"
          ++ lib.optional (
            cfg.workloadIdentity.signingKeyFile != null
          ) "workload-identity-key:${cfg.workloadIdentity.signingKeyFile}"
          ++ lib.optional (
            !cfg.database.createLocally && cfg.database.urlFile != null
          ) "db-url:${cfg.database.urlFile}"
          ++ lib.mapAttrsToList (
            repoName: path: "effects-secret__${cleanUpRepoName repoName}:${path}"
          ) cfg.effects.perRepoSecretFiles
          ++ lib.mapAttrsToList (
            repoName: repo: "pull-based__${cleanUpRepoName repoName}:${repo.sshPrivateKeyFile}"
          ) (lib.filterAttrs (_: repo: repo.sshPrivateKeyFile != null) cfg.pullBased.repositories);

        # Hardening. The eval/effects sandboxes use unprivileged bwrap, so
        # namespace creation must stay allowed and the syscall filter must
        # include the mount family (used inside the new user namespace).
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        # Delegated cgroup subtree: the service caps each evaluation's
        # process tree with its own memory.max leaf (no polkit needed).
        Delegate = "memory";
        ProtectControlGroups = false;
        ProtectKernelModules = true;
        # ProtectKernelTunables/Logs, ProtectProc and ProtectHostname
        # overmount /proc paths; a fresh proc mount in bwrap's user
        # namespace is then rejected by the kernel, so they stay off.
        ProtectKernelTunables = false;
        ProtectKernelLogs = false;
        ProtectClock = true;
        ProtectProc = "default";
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
        ];
        # bwrap --unshare-all needs every type incl. cgroup.
        RestrictNamespaces = "user mnt pid net ipc uts cgroup";
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          # bwrap: new namespaces plus mounts inside them.
          "@mount"
          "unshare"
          "setns"
          # bwrap --hostname; only affects its own UTS namespace.
          "sethostname"
        ];
        ReadWritePaths = [
          "/nix/var/nix/gcroots/per-user/nixbot"
        ]
        ++ lib.optional (cfg.outputsPath != null) cfg.outputsPath;
      };
    };

    services.postgresql = lib.mkIf cfg.database.createLocally {
      enable = true;
      ensureDatabases = [ "nixbot" ];
      ensureUsers = [
        {
          name = "nixbot";
          ensureDBOwnership = true;
        }
      ];
    };

    services.nginx = lib.mkIf cfg.nginx.enable {
      enable = true;
      virtualHosts.${cfg.domain} = {
        forceSSL = lib.mkIf cfg.nginx.enableACME true;
        enableACME = lib.mkIf cfg.nginx.enableACME true;
        locations = {
          "/" = {
            proxyPass = "http://unix:${webUnixSocket}";
            extraConfig = ''
              # Webhook deliveries can exceed nginx's 1m default
              # (GitHub caps payloads at 25 MB).
              client_max_body_size 25m;
              proxy_connect_timeout 120s;
              proxy_send_timeout 120s;
              # Long timeout keeps SSE log streams alive.
              proxy_read_timeout 3600s;
              # Buffering would stall SSE.
              proxy_buffering off;
            '';
          };
        }
        // lib.optionalAttrs (cfg.outputsPath != null) {
          "/nix-outputs/" = {
            # alias on a "/"-terminated location must itself end in
            # "/" or nginx mangles the mapped path.
            alias = lib.removeSuffix "/" cfg.outputsPath + "/";
            extraConfig = ''
              charset utf-8;
              autoindex on;
            '';
          };
        };
      };
    };

    systemd.tmpfiles.rules = [
      "d /nix/var/nix/gcroots/per-user/nixbot 0755 nixbot nixbot - -"
      # Drop gc-roots of builds that have not been refreshed in 90 days.
      "e /nix/var/nix/gcroots/per-user/nixbot/* - - - 90d -"
    ]
    ++ lib.optionals (cfg.outputsPath != null) [
      "d ${cfg.outputsPath} 0755 nixbot nixbot - -"
      # Recursively fix ownership so files created by a previous service user
      # (e.g. after the buildbot -> nixbot migration) stay writable.
      "Z ${cfg.outputsPath} - nixbot nixbot - -"
    ];
  };
}
