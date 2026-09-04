import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Comment on the pull request this nixbot effect runs for"
    )
    parser.add_argument(
        "--replace-marker",
        metavar="ID",
        help="edit the previous comment posted with this marker instead",
    )
    parser.add_argument("body", nargs="?", help="comment text, default: stdin")
    args = parser.parse_args()
    url = os.environ.get("NIXBOT_API_URL")
    token = os.environ.get("NIXBOT_API_TOKEN")
    body = args.body if args.body is not None else sys.stdin.read()
    if not url or not token:
        # Local `nbo effects run`: show instead of failing the effect.
        print("nixbot-pr-comment (not under nixbot):", file=sys.stderr)
        print(body, file=sys.stderr)
        return
    payload = {"body": body, "marker": args.replace_marker}
    request = urllib.request.Request(
        f"{url}/api/v1/pr-comment",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60):
            pass
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        sys.exit(f"nixbot-pr-comment: {e}: {detail}")


main()
