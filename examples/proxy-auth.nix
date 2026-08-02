{ ... }:
{
  # Header-based auth: an authenticating reverse proxy in front of the
  # nixbot vhost sets the username header; nixbot trusts it as the user
  # identity ("proxy:<username>"). The proxy MUST set or strip the
  # header on every request, otherwise clients can impersonate any user.
  services.nixbot = {
    proxyAuthHeader = "X-Remote-User";
    admins = [ "proxy:alice" ];
  };

  # Example: nginx in front terminates auth (e.g. via auth_request or
  # basic auth) and forwards the verified username to nixbot's vhost.
  services.nginx.virtualHosts."nixbot.thalheim.io".locations."/".extraConfig = ''
    auth_basic "nixbot";
    auth_basic_user_file /var/lib/secrets/nixbot-htpasswd; # FIXME: use a secret manager
    proxy_set_header X-Remote-User $remote_user;
  '';
}
