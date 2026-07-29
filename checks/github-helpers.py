# Shared testScript helpers for the fake-GitHub based checks
# (prepended to the testScript, see github-node.nix).
import hashlib
import hmac
import json
import shlex


def push_webhook(node, ref="refs/heads/master", message="initial commit"):
    """Deliver a signed push webhook for the current tip of the bare repo."""
    sha = node.succeed("git -C /var/lib/test-repo rev-parse master").strip()
    body = json.dumps(
        {
            "ref": ref,
            "after": sha,
            "repository": {"id": 1, "default_branch": "master"},
            "head_commit": {"message": message},
        }
    ).encode()
    sig = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    node.succeed(
        "curl --fail -s -X POST http://127.0.0.1:8010/webhooks/github "
        "-H 'Content-Type: application/json' "
        "-H 'X-GitHub-Event: push' "
        "-H 'X-GitHub-Delivery: test-delivery-1' "
        f"-H 'X-Hub-Signature-256: sha256={sig}' "
        f"-d {shlex.quote(body.decode())}"
    )
    return sha


def completed_check_runs(node):
    """Conclusions of completed check runs posted to the fake GitHub, by name."""
    out = node.execute("cat /var/lib/fake-github/check_runs.jsonl")[1]
    check_runs = [json.loads(line) for line in out.splitlines() if line]
    print(check_runs)
    return {
        cr["name"]: cr.get("conclusion")
        for cr in check_runs
        if cr.get("status") == "completed"
    }
