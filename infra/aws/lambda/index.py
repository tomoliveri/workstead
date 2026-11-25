"""Workstead index: a tiny always-free API in front of DynamoDB and S3.

Agents call it about once per task cycle instead of polling:

GET  /api/tips?repo=URL[&head=SHA&pubkey=HEX]
     Unverified PatchSubmitted vertices (Tangle tips), per-hour Merkle roots of
     what the index holds, banned keys, and (with pubkey and head) a
     deterministic verification assignment: sha256(pubkey + head) picks which
     tip you verify, so verifiers can't choose their friends' patches.
POST /api/vertex      a signed milestone event (task, receipt, settled, ban,
                      endorsement) to index. Verified, PoW-checked, rate-limited.
POST /api/artifact    NIP-98-signed request for a presigned upload slot for a
                      log/patch bundle (content-addressed, <= 2 MB, deleted
                      after 7 days). Only the repository's trusted keys.
GET  /api/artifact/<sha256>   a short-lived download link.

Behind CloudFront (origin access control, cached tips, edge rate limits)
with reserved concurrency 5, so a runaway loop can't run up a bill.
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.request

import boto3

from workstead import nostr as N
from workstead import dag as D

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE"])
S3 = boto3.client("s3")
BUCKET = os.environ["BUCKET"]
TTL_DAYS = 90
MAX_ART = 2 * 1024 * 1024
DAILY_ART_BYTES = int(os.environ.get("DAILY_ART_BYTES", str(1024 ** 3)))
REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_CFG = {}


def resp(code, body, cache=0):
    h = {"content-type": "application/json", "cache-control": f"public, max-age={cache}" if cache else "no-store"}
    return {"statusCode": code, "headers": h, "body": json.dumps(body)}


def repo_cfg(repo):
    """The repository's own workstead.json (publishers, trusted agents), cached briefly."""
    hit = _CFG.get(repo)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    owner_name = repo.split("github.com/", 1)[1]
    cfg = {"relay": {"publishers": [], "trusted_agents": [], "pow_bits": 8}}
    for branch in ("main", "master"):
        try:
            with urllib.request.urlopen(f"https://raw.githubusercontent.com/{owner_name}/{branch}/workstead.json",
                                        timeout=4) as r:
                data = json.loads(r.read().decode())
            rel = data.get("relay", {}) if isinstance(data, dict) else {}
            cfg["relay"].update({k: rel[k] for k in ("publishers", "trusted_agents", "pow_bits") if k in rel})
            break
        except Exception:
            continue  # try the next one
    _CFG[repo] = (time.time(), cfg)
    return cfg


def keys(cfg, name):
    from workstead import schnorr
    out = set()
    for k in cfg["relay"].get(name, []):
        try:
            out.add(schnorr.parse_pubkey(k))
        except ValueError:
            pass  # best effort: the connection or cache is optional here
    return out


def rate_ok(scope, limit, window=60):
    slot = int(time.time() // window)
    try:
        TABLE.update_item(Key={"pk": f"rl#{scope}", "sk": str(slot)},
                          UpdateExpression="ADD c :one SET exp = :exp",
                          ConditionExpression="attribute_not_exists(c) OR c < :lim",
                          ExpressionAttributeValues={":one": 1, ":lim": limit, ":exp": int(time.time()) + window * 2})
        return True
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def put_vertex(ev):
    repo = N.tag(ev, "r", "")
    if not REPO_RE.match(repo):
        return resp(400, {"error": "missing or bad r tag"})
    if ev["kind"] not in (N.K_TASK, N.K_RECEIPT, N.K_SETTLED, N.K_BAN, N.K_ENDORSE) or N.APP_TAG not in ev["tags"]:
        return resp(400, {"error": "only Workstead milestone events are indexed"})
    if not N.verify(ev):
        return resp(400, {"error": "bad id or signature"})
    cfg = repo_cfg(repo)
    pubs = keys(cfg, "publishers")
    if ev["kind"] in (N.K_TASK, N.K_SETTLED, N.K_BAN, N.K_ENDORSE) and ev["pubkey"] not in pubs:
        return resp(403, {"error": "only the repository's publishers may index that kind"})
    if ev["kind"] == N.K_RECEIPT and not N.pow_ok(ev, int(cfg["relay"].get("pow_bits", 8))):
        return resp(400, {"error": "proof of work required"})
    if not rate_ok(ev["pubkey"], 30):
        return resp(429, {"error": "slow down"})
    exp = int(N.tag(ev, "expiration") or ev["created_at"] + TTL_DAYS * 86400)
    item = {"pk": f"v#{repo}", "sk": f"{ev['created_at']:012d}#{ev['id']}", "id": ev["id"], "kind": ev["kind"],
            "pubkey": ev["pubkey"], "created_at": ev["created_at"], "exp": exp,
            "parents": [t[1] for t in N.tags(ev, "parent")][:2], "event": json.dumps(ev, separators=(",", ":"))}
    if ev["kind"] == N.K_BAN:
        for t in N.tags(ev, "p"):
            TABLE.put_item(Item={"pk": f"ban#{repo}", "sk": t[1], "reason": N.tag(ev, "reason", ""), "exp": exp})
    TABLE.put_item(Item=item)
    return resp(200, {"ok": True, "id": ev["id"]})


def vertices(repo, since):
    from boto3.dynamodb.conditions import Key
    out, kw = [], {"KeyConditionExpression": Key("pk").eq(f"v#{repo}") & Key("sk").gte(f"{since:012d}")}
    while True:
        r = TABLE.query(**kw)
        out += [json.loads(i["event"]) for i in r["Items"] if int(i.get("exp", 0)) > time.time()]
        if "LastEvaluatedKey" not in r or len(out) > 5000:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def get_tips(q):
    repo = q.get("repo", "")
    if not REPO_RE.match(repo):
        return resp(400, {"error": "repo=https://github.com/owner/name"})
    evs = vertices(repo, int(time.time()) - 14 * 86400)
    receipts = [e for e in evs if e["kind"] == N.K_RECEIPT]
    verified = {(N.tag(r, "e"), N.tag(r, "output")) for r in receipts if N.tag(r, "role") == "verify"}
    tips = [e for e in D.tips(evs) if e["kind"] == N.K_RECEIPT and N.tag(e, "role") == "produce"
            and (N.tag(e, "e"), N.tag(e, "output")) not in verified]
    from boto3.dynamodb.conditions import Key
    bans = [i["sk"] for i in TABLE.query(KeyConditionExpression=Key("pk").eq(f"ban#{repo}"))["Items"]
            if int(i.get("exp", 0)) > time.time()]
    body = {"repo": repo, "time": int(time.time()), "tips": [t["id"] for t in tips][:200],
            "roots": {str(k): v for k, v in D.bucket_roots(evs).items()}, "banned": bans}
    pk, head = q.get("pubkey", ""), q.get("head", "")
    if tips and re.fullmatch(r"[0-9a-f]{64}", pk) and re.fullmatch(r"[0-9a-f]{40}", head):
        mine = [t for t in tips if t["pubkey"] != pk] or tips
        idx = int(hashlib.sha256((pk + head).encode()).hexdigest(), 16) % len(mine)
        body["assignment"] = mine[idx]["id"]
        return resp(200, body)  # personalised: not cached
    return resp(200, body, cache=30)


def nip98(event, url, method):
    """NIP-98 HTTP auth: a fresh kind-27235 event signed over this URL and method."""
    try:
        ev = json.loads(base64.b64decode(event))
    except Exception:
        return None
    if not N.verify(ev) or ev.get("kind") != 27235 or abs(time.time() - ev["created_at"]) > 60:
        return None
    if N.tag(ev, "u") != url or N.tag(ev, "method", "").upper() != method:
        return None
    return ev["pubkey"]


def artifact(event, path, method):
    h = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if method == "GET":
        sha = path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return resp(400, {"error": "sha256 required"})
        url = S3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": f"art/{sha}"}, ExpiresIn=300)
        return {"statusCode": 302, "headers": {"location": url, "cache-control": "no-store"}, "body": ""}
    body = json.loads(event.get("body") or "{}")
    repo, sha, size = body.get("repo", ""), body.get("sha256", ""), body.get("size", 0)
    if not (REPO_RE.match(repo) and re.fullmatch(r"[0-9a-f]{64}", sha) and isinstance(size, int) and 0 < size <= MAX_ART):
        return resp(400, {"error": "repo, sha256 and size (<= 2 MB) required"})
    auth = h.get("authorization", "")
    pk = nip98(auth[6:], f"https://{h.get('host', '')}/api/artifact", "POST") if auth.startswith("Nostr ") else None
    cfg = repo_cfg(repo)
    if not pk or pk not in keys(cfg, "publishers") | keys(cfg, "trusted_agents"):
        return resp(403, {"error": "artifacts are for the repository's trusted keys (NIP-98 auth)"})
    if not rate_ok(f"art#{pk}", 20, 3600) or not rate_ok("art#bytes", DAILY_ART_BYTES // MAX_ART, 86400):
        return resp(429, {"error": "artifact quota reached"})
    post = S3.generate_presigned_post(BUCKET, f"art/{sha}", Fields={"Content-Type": "application/gzip"},
                                      Conditions=[["content-length-range", 1, MAX_ART], {"Content-Type": "application/gzip"}],
                                      ExpiresIn=300)
    return resp(200, {"upload": post, "get": f"/api/artifact/{sha}", "expires_days": 7})


def handler(event, context):
    rc = event.get("requestContext", {}).get("http", {})
    method, path = rc.get("method", "GET"), rc.get("path", "/")
    q = event.get("queryStringParameters") or {}
    try:
        if path.startswith("/api/tips") and method == "GET":
            return get_tips(q)
        if path.startswith("/api/vertex") and method == "POST":
            raw = event.get("body") or ""
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode()
            if len(raw) > 64000:
                return resp(413, {"error": "too large"})
            return put_vertex(json.loads(raw))
        if path.startswith("/api/artifact"):
            return artifact(event, path, method)
        if path.startswith("/api/health"):
            return resp(200, {"ok": True})
        return resp(404, {"error": "not found"})
    except (ValueError, KeyError, TypeError) as e:
        return resp(400, {"error": str(e)[:200]})
