import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Request a workload-identity ID token from nixbot"
    )
    parser.add_argument("audience")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the raw {token, expires_at} response",
    )
    args = parser.parse_args()
    url = os.environ.get("NIXBOT_ID_TOKEN_REQUEST_URL")
    token = os.environ.get("NIXBOT_ID_TOKEN_REQUEST_TOKEN")
    if not url or not token:
        sys.exit(
            "nixbot-id-token: no ID token endpoint available; "
            "declare the audience in the effect's idTokenAudiences"
        )
    request = urllib.request.Request(
        url,
        data=json.dumps({"audience": args.audience}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as e:
        sys.exit(f"nixbot-id-token: {e}: {e.read().decode(errors='replace')}")
    print(body if args.json else json.loads(body)["token"])


main()
