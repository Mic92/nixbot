# Workload identity for effects

nixbot can act as an OIDC issuer that mints short-lived ID tokens for running
effects. Relying parties (OpenBao/Vault, AWS/GCP/Azure federation,
[niks3](https://github.com/Mic92/niks3), ...) verify the tokens against nixbot's
JWKS and grant access based on the claims, so effects need no static deploy
secrets in `effects.perRepoSecretFiles`.

Enabled by default (`services.nixbot.workloadIdentity`); an effect only receives
tokens for audiences it declares.

## Declaring audiences

Set `idTokenAudiences` on the effect derivation (an argument of this repo's
effects-lib `mkEffect`, or a plain env attribute containing a JSON list):

```nix
effects-lib.mkEffect {
  name = "deploy";
  idTokenAudiences = [ "https://vault.example.com" ];
  inputs = [ pkgs.openbao pkgs.openssh ];
  effectScript = ''
    export VAULT_ADDR=https://vault.example.com

    # Authenticate to Vault with the nixbot-issued identity token.
    vault write -field=token auth/jwt/login \
      role=deploy jwt="$(nixbot-id-token https://vault.example.com)" > ~/.vault-token

    # Trade it for a short-lived SSH certificate and deploy with it:
    # no deploy key is stored in CI.
    ssh-keygen -t ed25519 -N "" -f ./id_deploy
    vault write -field=signed_key ssh/sign/deploy \
      public_key=@./id_deploy.pub valid_principals=deploy > ./id_deploy-cert.pub

    export NIX_SSHOPTS="-i ./id_deploy -o CertificateFile=./id_deploy-cert.pub"
    nixos-rebuild switch --flake .#web01 \
      --target-host deploy@web01.example.com \
      --use-remote-sudo
  '';
}
```

The matching Vault SSH CA setup is:

```
vault secrets enable ssh
vault write ssh/config/ca generate_signing_key=true
vault write ssh/roles/deploy \
  key_type=ca allow_user_certificates=true \
  allowed_users=deploy default_user=deploy ttl=10m
```

Inside the sandbox, `nixbot-id-token <audience>` prints a fresh ID token
(default lifetime 300 s). `nixbot-id-token <audience> --json` prints the raw
`{"token": ..., "expires_at": ...}` response, which is the format niks3's
`token-script` credential expects. Requests for audiences not listed in
`idTokenAudiences` are rejected. The env variables it uses
(`NIXBOT_ID_TOKEN_REQUEST_URL`, `NIXBOT_ID_TOKEN_REQUEST_TOKEN`) are only
present for effects that declare at least one audience.

Note the opt-in scopes tokens per effect, not per person: anyone who can get an
effect with `idTokenAudiences` to run in the repository can obtain its tokens.
Restrict relying-party policies with the claims below (in particular
`sub`/`ref`), and remember effects only run for pushes and pull requests
according to the repository's effects gating.

## Claims

Common claims: `iss` (the nixbot URL), `aud` (one requested audience per token),
`sub`, `event`, `forge`, `repository`, `repository_owner`, `effect`, plus
`iat`/`nbf`/`exp`/`jti`.

| event        | `sub`                                                 | extra claims                               |
| ------------ | ----------------------------------------------------- | ------------------------------------------ |
| push         | `repo:<forge>:<owner>/<repo>:ref:refs/heads/<branch>` | `ref`, `sha`, `build_id`                   |
| pull_request | `repo:<forge>:<owner>/<repo>:pull_request`            | `pr_number`, `base_ref`, `sha`, `build_id` |
| schedule     | `repo:<forge>:<owner>/<repo>:schedule:<name>`         | `schedule`                                 |

Pull-request tokens carry no `ref` claim: branch-based conditions (e.g. "only
refs/heads/main may deploy") therefore never match a PR run.

## Relying-party configuration

Discovery and JWKS documents are served unauthenticated at
`<url>/.well-known/openid-configuration` and `<url>/.well-known/jwks.json`.

Example niks3 provider:

```toml
[upload-oidc-providers.nixbot]
issuer = "https://ci.example.com"
audience = "https://cache.example.com"
bound_subject = "repo:github:acme/*:ref:refs/heads/main"
```

Example Vault/OpenBao JWT role for the deploy effect above (only pushes to main
of acme/widgets can log in; PR-triggered effects carry no `ref` claim and never
match):

```
vault write auth/jwt/config oidc_discovery_url=https://ci.example.com
vault write auth/jwt/role/deploy \
  role_type=jwt user_claim=sub \
  bound_audiences=https://vault.example.com \
  bound_subject=repo:github:acme/widgets:ref:refs/heads/main \
  token_policies=ssh-deploy token_ttl=5m
```

## Keys

Tokens are RS256-signed. Without `workloadIdentity.signingKeyFile`, nixbot
generates the key in its state directory and rotates it every `keyRotationDays`
(default 30); the previous key stays in the JWKS so in-flight tokens keep
verifying. An operator-provided key is used as-is and never rotated — rotate it
by replacing the file and restarting nixbot, which invalidates tokens signed
with the old key.
