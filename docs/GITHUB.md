# GitHub Integration

Buildbot-nix uses GitHub App authentication to integrate with GitHub
repositories. This enables automatic webhook setup, check-run reporting, and
secure authentication.

## Step 1: Create a GitHub App

1. Navigate to:
   - For personal accounts: `https://github.com/settings/apps/new`
   - For organizations:
     `https://github.com/organizations/<org>/settings/apps/new`

2. Configure the app with these settings:
   - **GitHub App Name**: `nixbot-<org>` (or any unique name)
   - **Homepage URL**: `https://nixbot.<your-domain>`
   - **Webhook**: Enable (Active) and set:
     - **Webhook URL**: `https://nixbot.<your-domain>/webhooks/github`
     - **Webhook secret**: the same value as `webhookSecretFile` below
   - **Callback URL** (optional, for OAuth):
     `https://nixbot.<your-domain>/auth/github/callback`

3. Set the required permissions:
   - **Repository Permissions:**
     - Contents: Read-only (to clone repositories), (read and write is required
       for effects to push to branches)
     - Checks: Read and write (to report build status as check runs)
     - Metadata: Read-only (basic repository info)
     - Pull requests: Read-only (required to subscribe to the pull_request
       event), (read and write is required for effects to create PRs)
   - **Organization Permissions** (if app is for an organization):
     - Members: Read-only (to verify organization membership for access control)
   - **Subscribe to events**: Push, Pull request, Check run, Check suite

   Note: when adding permissions to an existing app, every installation (your
   user account and each organization) must accept the new permissions under
   Settings → GitHub Apps → Configure before events are delivered.

4. After creating the app:
   - Note the **App ID**
   - Generate and download a **private key** (.pem file)

## Step 2: Configure nixbot

Add the GitHub configuration to your NixOS module:

```nix
services.nixbot = {
  enable = true;
  domain = "nixbot.example.com";  # Your nixbot domain
  github = {
    enable = true;
    appId = <your-app-id>;  # The numeric App ID
    appSecretKeyFile = "/path/to/private-key.pem";  # Path to the downloaded private key

    # OAuth credentials enable the GitHub login button
    oauthId = "<oauth-client-id>";
    oauthSecretFile = "/path/to/oauth-secret";

    # Optional: request the write-capable "repo" OAuth scope at login so
    # private repositories are visible to their members, and so the login
    # token carries the push access that drives the per-repo restart
    # button (see "OAuth scope and the restart button" below). GitHub has
    # no read-only repo scope: "repo" grants write access and nixbot
    # stores the token server-side for the session. Off by default; leave
    # it off unless the instance builds private repositories.
    # oauthPrivateRepoScope = true;

    # A random secret used to verify incoming webhooks from GitHub
    webhookSecretFile = "/path/to/webhook-secret";

    # Optional: only allow these owners/repositories to be built
    userAllowlist = [ "my-org" ];
    repoAllowlist = [ "other-org/repo" ];

    # One-shot import: repositories with this topic are enabled on first
    # startup with an empty database; afterwards manage projects in the web UI
    topic = "build-with-buildbot";
  };
};
```

## Step 3: Install the GitHub App

1. Go to your app's settings page
2. Click "Install App" and choose which repositories to grant access
3. The app needs access to all repositories you want to build with nixbot

## Step 4: Repository Configuration

For each repository you want to build:

1. **Enable the project**:
   - Toggle the project on in the web UI (as admin)

2. **Webhook delivery**:
   - GitHub delivers push, pull_request, check_run and check_suite events
     through the App-level webhook configured in Step 1; no per-repository
     webhooks are created.
   - The endpoint is `https://nixbot.<your-domain>/webhooks/github` (the legacy
     `/change_hook/github` path also works).

## How It Works

- **Authentication**: Uses GitHub App JWT tokens for API access and installation
  tokens for repository-specific operations
- **Project Discovery**: Automatically discovers repositories the app has access
  to (restricted by `userAllowlist`/`repoAllowlist` if set); discovered projects
  are built once enabled in the web UI
- **Webhook Delivery**: Push, pull_request, check_run and check_suite events
  arrive via the GitHub App webhook; the payload signature is verified with the
  webhook secret
- **Status Updates**: Reports build status as Check Runs (with markdown log
  excerpts and a working Re-run button) on commits and pull requests
- **Access Control**:
  - Admins: Configured users can reload projects and manage builds
  - Repo writers: Users with write access to the repo can restart/cancel its
    builds
  - PR authors: Can restart/cancel the builds of their own pull request

### Repository permissions

nixbot maps a user's GitHub collaborator permission onto its own roles. Each
level includes the ones above it:

| GitHub permission | Allowed in nixbot                                |
| ----------------- | ------------------------------------------------ |
| read              | view the project, including private repositories |
| write / maintain  | restart and cancel the project's builds          |
| admin             | enable or disable the project                    |

The lookup uses the App's own credentials
(`GET /repos/{owner}/{repo}/collaborators/{user}/permission`) and only needs the
Metadata (read) permission that every GitHub App has, so no extra configuration
is required. The user's login token merely proves identity (`read:user` scope),
which is why the same rules apply to web sessions and personal API tokens.

Independent of repository permissions, instance admins can do everything, and PR
authors can restart or cancel the builds of their own pull requests.

## Troubleshooting

- **Projects not appearing**: Check that:
  - The GitHub App is installed for the repository
  - The repository is not excluded by `userAllowlist`/`repoAllowlist`
  - Reload projects manually through the web UI

- **Project appears but nothing builds**: Enable the project in the web UI

- **No builds on push**: Verify the App webhook is Active, its URL points to
  `https://nixbot.<your-domain>/webhooks/github`, and its secret matches
  `webhookSecretFile`. Check recent deliveries under the app's "Advanced" tab.

- **Authentication issues**: Ensure the private key file is readable by the
  nixbot service
