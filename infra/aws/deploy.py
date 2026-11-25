#!/usr/bin/env python3
"""Deploy the Workstead seed infrastructure on AWS (free-tier friendly).

    python3 infra/aws/deploy.py up        # create or converge everything
    python3 infra/aws/deploy.py status    # health and ids
    python3 infra/aws/deploy.py brake     # what the budget brake does, by hand
    python3 infra/aws/deploy.py resume    # undo the brake

Uses the AWS CLI (credentials from the usual places). Everything is tagged
project=workstead, lives in its own VPC, and is idempotent.

What it builds (docs/aws.md has the why):

* Seed relay: an immutable EC2 t4g.small (Graviton, free trial through
  2026-12-31) running `workstead.relayd` under a locked-down systemd unit. No
  IAM role, no SSH key, no SSM, no public IPv4, IMDS disabled after boot, and
  a security group with no egress at all once booted: even fully compromised
  it can't reach anything. Ingress only from CloudFront's IPv6 origin range,
  plus a secret origin header so other CloudFront distributions can't reach it.
* CloudFront in front: free TLS (wss://<id>.cloudfront.net), DDoS protection
  (Shield Standard), caching for the index.
* Index: Lambda (arm64, reserved concurrency 5) + DynamoDB (provisioned 5/5,
  TTL) for Tangle tips, verification assignments and bans; S3 for 7-day
  artifact spillover. Lambda is reachable only through CloudFront (OAC).
* Guardrails: a tag-scoped AWS Budget ($10/month; alerts at $1, $5, $10) and
  a brake Lambda that throttles the index to zero and stops the relay at $10.
"""
from __future__ import annotations

import ast
import hashlib
import io
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools" / "ws" / "workstead"
HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
NAME = "workstead-seed"
TAG = {"Key": "project", "Value": "workstead"}
CF_IPV6_ORIGIN_LIST = "com.amazonaws.global.ipv6.cloudfront.origin-facing"
RELAY_MODULES = ["__init__", "relayd", "relay", "nostr", "schnorr", "dag", "logs"]
LAMBDA_MODULES = ["__init__", "nostr", "schnorr", "dag", "logs"]
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_AND_CF_HEADERS = "33f36d7e-f396-46d9-90e0-52428a34d9dc"
ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
BUDGET_EMAIL = os.environ.get("WS_BUDGET_EMAIL", "")
PUBLIC_RELAYS = ["wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net", "wss://offchain.pub"]


def say(msg: str) -> None:
    print(f"workstead-aws: {msg}", flush=True)


def aws(*args: str, check: bool = True, input_: str = None):
    cmd = ["aws", *args, "--output", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True, input=input_)
    if r.returncode != 0:
        if check:
            raise RuntimeError(f"aws {' '.join(args[:3])}: {r.stderr.strip()[-600:]}")
        return None
    return json.loads(r.stdout) if r.stdout.strip() else {}


def tags_spec(kind: str, name: str):
    return f"ResourceType={kind},Tags=[{{Key=project,Value=workstead}},{{Key=Name,Value={name}}}]"


def load_state():
    return json.loads(STATE.read_text()) if STATE.is_file() else {}


def save_state(st) -> None:
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True))


def ctx():
    region = os.environ.get("AWS_REGION") or subprocess.run(["aws", "configure", "get", "region"], capture_output=True,
                                                            text=True).stdout.strip() or "ap-southeast-2"
    os.environ["AWS_REGION"] = region
    account = aws("sts", "get-caller-identity")["Account"]
    return region, account


# --- code bundles --------------------------------------------------------------------


def _strip(src: str) -> str:
    t = ast.parse(src)
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module, ast.AsyncFunctionDef)) and n.body and \
                isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant) \
                and isinstance(n.body[0].value.value, str):
            n.body = n.body[1:] or [ast.Pass()]
    return ast.unparse(t)


def relay_bundle() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tf:
        for m in RELAY_MODULES:
            data = _strip((PKG / f"{m}.py").read_text()).encode()
            ti = tarfile.TarInfo(f"workstead/{m}.py")
            ti.size, ti.mtime, ti.mode = len(data), 0, 0o444
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def lambda_zip(handler: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "lambda" / handler, handler)
        if handler == "index.py":
            for m in LAMBDA_MODULES:
                z.write(PKG / f"{m}.py", f"workstead/{m}.py")
    return buf.getvalue()


# --- S3 ---------------------------------------------------------------------------------


def ensure_bucket(region, account):
    b = f"workstead-artifacts-{account}-{region}"
    if aws("s3api", "head-bucket", "--bucket", b, check=False) is None:
        args = ["s3api", "create-bucket", "--bucket", b]
        if region != "us-east-1":
            args += ["--create-bucket-configuration", f"LocationConstraint={region}"]
        aws(*args)
        say(f"created bucket {b}")
    aws("s3api", "put-public-access-block", "--bucket", b, "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true")
    aws("s3api", "put-bucket-ownership-controls", "--bucket", b, "--ownership-controls",
        "Rules=[{ObjectOwnership=BucketOwnerEnforced}]")
    aws("s3api", "put-bucket-encryption", "--bucket", b, "--server-side-encryption-configuration",
        json.dumps({"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}))
    aws("s3api", "put-bucket-lifecycle-configuration", "--bucket", b, "--lifecycle-configuration", json.dumps({"Rules": [
        {"ID": "artifacts-7-days", "Status": "Enabled", "Filter": {"Prefix": "art/"}, "Expiration": {"Days": 7}},
        {"ID": "abort-uploads", "Status": "Enabled", "Filter": {"Prefix": ""},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}},
        {"ID": "old-code", "Status": "Enabled", "Filter": {"Prefix": "code/"}, "Expiration": {"Days": 30}}]}))
    aws("s3api", "put-bucket-policy", "--bucket", b, "--policy", json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "TLSOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
         "Resource": [f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*"],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}))
    aws("s3api", "put-bucket-tagging", "--bucket", b, "--tagging", "TagSet=[{Key=project,Value=workstead}]")
    return b


def upload_code(bucket):
    data = relay_bundle()
    sha = hashlib.sha256(data).hexdigest()
    key = f"code/relayd-{sha[:16]}.tgz"
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as f:
        f.write(data)
    aws("s3api", "put-object", "--bucket", bucket, "--key", key, "--body", f.name)
    os.unlink(f.name)
    url = subprocess.run(["aws", "s3", "presign", f"s3://{bucket}/{key}", "--expires-in", "10800"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return sha, key, url


# --- network ---------------------------------------------------------------------------------


def find(kind, filters):
    res = aws("ec2", f"describe-{kind}", "--filters", *filters)
    key = {"vpcs": "Vpcs", "subnets": "Subnets", "internet-gateways": "InternetGateways",
           "route-tables": "RouteTables", "security-groups": "SecurityGroups", "vpc-endpoints": "VpcEndpoints"}[kind]
    return res[key]


def ensure_network(region, bucket):
    tagf = ["Name=tag:project,Values=workstead", f"Name=tag:Name,Values={NAME}"]
    vpcs = find("vpcs", tagf)
    if vpcs:
        vpc = vpcs[0]["VpcId"]
    else:
        vpc = aws("ec2", "create-vpc", "--cidr-block", "10.77.0.0/24", "--amazon-provided-ipv6-cidr-block",
                  "--tag-specifications", tags_spec("vpc", NAME))["Vpc"]["VpcId"]
        aws("ec2", "wait", "vpc-available", "--vpc-ids", vpc)
        say(f"created VPC {vpc} (isolated: no peering, no NAT)")
    v6 = None
    for _ in range(30):
        assoc = aws("ec2", "describe-vpcs", "--vpc-ids", vpc)["Vpcs"][0].get("Ipv6CidrBlockAssociationSet", [])
        v6 = next((a["Ipv6CidrBlock"] for a in assoc if a["Ipv6CidrBlockState"]["State"] == "associated"), None)
        if v6:
            break
        time.sleep(2)
    igws = find("internet-gateways", tagf)
    if igws:
        igw = igws[0]["InternetGatewayId"]
    else:
        igw = aws("ec2", "create-internet-gateway", "--tag-specifications",
                  tags_spec("internet-gateway", NAME))["InternetGateway"]["InternetGatewayId"]
        aws("ec2", "attach-internet-gateway", "--internet-gateway-id", igw, "--vpc-id", vpc)
    subs = find("subnets", tagf)
    if subs:
        subnet = subs[0]["SubnetId"]
    else:
        net = ipaddress.IPv6Network(v6)
        first64 = str(next(net.subnets(new_prefix=64)))
        az = aws("ec2", "describe-availability-zones")["AvailabilityZones"][0]["ZoneName"]
        subnet = aws("ec2", "create-subnet", "--vpc-id", vpc, "--cidr-block", "10.77.0.0/26", "--ipv6-cidr-block",
                     first64, "--availability-zone", az, "--tag-specifications", tags_spec("subnet", NAME))["Subnet"]["SubnetId"]
        aws("ec2", "modify-subnet-attribute", "--subnet-id", subnet, "--assign-ipv6-address-on-creation")
        aws("ec2", "modify-subnet-attribute", "--subnet-id", subnet, "--no-map-public-ip-on-launch")
    rts = find("route-tables", tagf)
    if rts:
        rt = rts[0]["RouteTableId"]
    else:
        rt = aws("ec2", "create-route-table", "--vpc-id", vpc, "--tag-specifications",
                 tags_spec("route-table", NAME))["RouteTable"]["RouteTableId"]
        # IPv6 only: there is deliberately no IPv4 default route (no IPv4 internet at all).
        aws("ec2", "create-route", "--route-table-id", rt, "--destination-ipv6-cidr-block", "::/0", "--gateway-id", igw)
        aws("ec2", "associate-route-table", "--route-table-id", rt, "--subnet-id", subnet)
    eps = find("vpc-endpoints", ["Name=tag:project,Values=workstead", f"Name=vpc-id,Values={vpc}"])
    policy = json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{bucket}/code/*"}]})
    if eps:
        aws("ec2", "modify-vpc-endpoint", "--vpc-endpoint-id", eps[0]["VpcEndpointId"], "--policy-document", policy)
    else:
        aws("ec2", "create-vpc-endpoint", "--vpc-id", vpc, "--service-name", f"com.amazonaws.{region}.s3",
            "--route-table-ids", rt, "--policy-document", policy,
            "--tag-specifications", tags_spec("vpc-endpoint", NAME))
    return vpc, subnet


def prefix_list(name):
    pls = aws("ec2", "describe-managed-prefix-lists", "--filters", f"Name=prefix-list-name,Values={name}")["PrefixLists"]
    return pls[0]["PrefixListId"]


def ensure_sg(region, vpc):
    sgs = find("security-groups", ["Name=tag:project,Values=workstead", f"Name=vpc-id,Values={vpc}",
                                   f"Name=group-name,Values={NAME}"])
    if sgs:
        sg = sgs[0]["GroupId"]
    else:
        sg = aws("ec2", "create-security-group", "--group-name", NAME, "--vpc-id", vpc, "--description",
                 "Workstead seed relay: CloudFront IPv6 in on 8080, no egress",
                 "--tag-specifications", tags_spec("security-group", NAME))["GroupId"]
        cf = prefix_list(CF_IPV6_ORIGIN_LIST)
        aws("ec2", "authorize-security-group-ingress", "--group-id", sg, "--ip-permissions", json.dumps([{
            "IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080,
            "PrefixListIds": [{"PrefixListId": cf, "Description": "CloudFront origin-facing (IPv6)"}]}]))
    revoke_egress(sg)
    return sg


def revoke_egress(sg, keep_s3_for_boot: str = ""):
    perms = aws("ec2", "describe-security-groups", "--group-ids", sg)["SecurityGroups"][0]["IpPermissionsEgress"]
    for p in perms:
        if keep_s3_for_boot and any(x.get("PrefixListId") == keep_s3_for_boot for x in p.get("PrefixListIds", [])):
            continue
        aws("ec2", "revoke-security-group-egress", "--group-id", sg, "--ip-permissions", json.dumps([p]))


def allow_boot_egress(sg, region):
    s3pl = prefix_list(f"com.amazonaws.{region}.s3")
    perms = aws("ec2", "describe-security-groups", "--group-ids", sg)["SecurityGroups"][0]["IpPermissionsEgress"]
    if not any(any(x.get("PrefixListId") == s3pl for x in p.get("PrefixListIds", [])) for p in perms):
        aws("ec2", "authorize-security-group-egress", "--group-id", sg, "--ip-permissions", json.dumps([{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "PrefixListIds": [{"PrefixListId": s3pl, "Description": "boot-time code fetch only (removed after boot)"}]}]))
    return s3pl


# --- the relay instance -------------------------------------------------------------------


def origin_secret():
    p = aws("ssm", "get-parameter", "--name", "/workstead/seed/origin-secret", "--with-decryption", check=False)
    if p:
        return p["Parameter"]["Value"]
    s = secrets.token_urlsafe(32)
    aws("ssm", "put-parameter", "--name", "/workstead/seed/origin-secret", "--type", "SecureString", "--value", s,
        "--tags", "Key=project,Value=workstead")
    return s


UNIT = """[Unit]
Description=Workstead seed relay (relayd)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 -m workstead.relayd --host :: --port 8080 --pow 8
Environment=PYTHONPATH=/opt/workstead PYTHONDONTWRITEBYTECODE=1 WS_CLIENT_IP_HEADER=cloudfront-viewer-address
Environment=WS_REQUIRE_ORIGIN=1
# Read by systemd as root before privileges drop; the file is root-only (0600).
EnvironmentFile=/etc/workstead/relayd.env
DynamicUser=yes
StateDirectory=workstead-relay
Restart=always
RestartSec=2
# --- sandbox: no privileges, no writable system, no devices, no kernel knobs
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
PrivateUsers=yes
ProtectHostname=yes
ProtectClock=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectProc=invisible
ProcSubset=pid
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RemoveIPC=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources @mount @debug @cpu-emulation @obsolete @raw-io @reboot @swap @module
UMask=0077
# Never talk to the instance metadata service, link-local or private ranges.
IPAddressDeny=169.254.0.0/16 fd00:ec2::/32 fe80::/10 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10
MemoryMax=1200M
TasksMax=4096
LimitNOFILE=8192

[Install]
WantedBy=multi-user.target
"""


def user_data(url: str, sha: str, secret: str) -> str:
    return f"""#!/bin/bash
# Workstead seed relay. Immutable: to change anything, deploy.py replaces the instance.
set -euo pipefail
systemctl disable --now sshd.service sshd.socket amazon-ssm-agent.service 2>/dev/null || true
systemctl mask sshd.service amazon-ssm-agent.service 2>/dev/null || true
cat > /etc/sysctl.d/90-workstead.conf <<'EOF'
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
kernel.unprivileged_bpf_disabled = 1
kernel.yama.ptrace_scope = 3
net.ipv4.conf.all.send_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
EOF
sysctl --system >/dev/null
mkdir -p /opt/workstead /etc/workstead
curl -fsS --retry 5 --max-time 60 -o /tmp/relayd.tgz '{url}'
echo '{sha}  /tmp/relayd.tgz' | sha256sum -c -
tar -xzf /tmp/relayd.tgz -C /opt/workstead && rm -f /tmp/relayd.tgz
chown -R root:root /opt/workstead && chmod -R a-w,a+rX /opt/workstead
umask 077
printf 'WS_ORIGIN_SECRET=%s\n' '{secret}' > /etc/workstead/relayd.env
chmod 600 /etc/workstead/relayd.env
cat > /etc/systemd/system/workstead-relayd.service <<'EOF'
{UNIT}EOF
systemctl daemon-reload
systemctl enable --now workstead-relayd.service
sleep 5
# No SSH by design: report to the serial console (aws ec2 get-console-output).
{{ echo "=== workstead-relayd status ==="; systemctl is-active workstead-relayd.service;
   journalctl -u workstead-relayd.service -n 20 --no-pager; }} > /dev/console 2>&1 || true
"""


def instances():
    res = aws("ec2", "describe-instances", "--filters", "Name=tag:project,Values=workstead", f"Name=tag:Name,Values={NAME}",
              "Name=instance-state-name,Values=pending,running,stopping,stopped")
    return [i for r in res["Reservations"] for i in r["Instances"]]


def launch(subnet, sg, url, sha, secret):
    ud = user_data(url, sha, secret)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(ud)
    inst = aws("ec2", "run-instances", "--image-id",
               "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
               "--instance-type", "t4g.small", "--subnet-id", subnet, "--security-group-ids", sg,
               "--ipv6-address-count", "1", "--credit-specification", "CpuCredits=standard",
               "--metadata-options", "HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled,"
                                     "HttpProtocolIpv6=enabled,InstanceMetadataTags=disabled",
               "--block-device-mappings", json.dumps([{"DeviceName": "/dev/xvda", "Ebs": {
                   "VolumeSize": 8, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}]),
               "--user-data", f"file://{f.name}",
               "--tag-specifications", tags_spec("instance", NAME), tags_spec("volume", NAME),
               tags_spec("network-interface", NAME),
               "--count", "1")["Instances"][0]
    os.unlink(f.name)
    iid = inst["InstanceId"]
    aws("ec2", "create-tags", "--resources", iid, "--tags", f"Key=code,Value={sha[:16]}")
    aws("ec2", "wait", "instance-running", "--instance-ids", iid)
    say(f"launched {iid} (code {sha[:12]})")
    return iid


def ipv6_of(iid):
    i = aws("ec2", "describe-instances", "--instance-ids", iid)["Reservations"][0]["Instances"][0]
    for eni in i["NetworkInterfaces"]:
        for a in eni.get("Ipv6Addresses", []):
            return a["Ipv6Address"]
    raise RuntimeError("instance has no IPv6 address")


def origin_host(ip6: str) -> str:
    # sslip.io resolves "<ipv6 with - for :>.sslip.io" to that address (AAAA), giving
    # CloudFront the hostname it needs without a DNS zone. Events are signed and
    # verified end to end, so the resolver isn't trusted with integrity.
    return ipaddress.IPv6Address(ip6).compressed.replace(":", "-") + ".sslip.io"


def lockdown(iid, sg):
    revoke_egress(sg)
    aws("ec2", "modify-instance-metadata-options", "--instance-id", iid, "--http-endpoint", "disabled")
    say(f"locked down {iid}: security group has no egress, metadata service disabled")


# --- CloudFront ----------------------------------------------------------------------------


def distribution_config(st, relay_host, secret, index_host=None, oac=None, cache_policy=None):
    origins = [{"Id": "relay", "DomainName": relay_host, "OriginPath": "",
                "CustomHeaders": {"Quantity": 1, "Items": [{"HeaderName": "X-Workstead-Origin", "HeaderValue": secret}]},
                "CustomOriginConfig": {"HTTPPort": 8080, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
                                       "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                                       "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 60,
                                       # the relay has no IPv4 at all: CloudFront must connect over IPv6
                                       "IpAddressType": "ipv6"},
                "ConnectionAttempts": 3, "ConnectionTimeout": 10, "OriginShield": {"Enabled": False},
                "OriginAccessControlId": ""}]
    behaviors = {"Quantity": 0}
    if index_host:
        origins.append({"Id": "index", "DomainName": index_host, "OriginPath": "",
                        "CustomHeaders": {"Quantity": 0},
                        "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only",
                                               "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                                               "OriginReadTimeout": 30, "OriginKeepaliveTimeout": 5},
                        "ConnectionAttempts": 3, "ConnectionTimeout": 10, "OriginShield": {"Enabled": False},
                        "OriginAccessControlId": oac})
        behaviors = {"Quantity": 1, "Items": [{
            "PathPattern": "/api/*", "TargetOriginId": "index", "ViewerProtocolPolicy": "https-only",
            "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            "CachePolicyId": cache_policy, "OriginRequestPolicyId": ALL_VIEWER_EXCEPT_HOST, "Compress": True,
            "SmoothStreaming": False, "FieldLevelEncryptionId": "",
            "LambdaFunctionAssociations": {"Quantity": 0}, "FunctionAssociations": {"Quantity": 0},
            "TrustedSigners": {"Enabled": False, "Quantity": 0}, "TrustedKeyGroups": {"Enabled": False, "Quantity": 0}}]}
    return {
        "CallerReference": st.get("cf_ref") or f"workstead-{int(time.time())}",
        "Comment": "Workstead seed relay and index", "Enabled": True, "PriceClass": "PriceClass_All",
        "HttpVersion": "http2", "IsIPV6Enabled": True, "DefaultRootObject": "",
        "Aliases": {"Quantity": 0}, "Origins": {"Quantity": len(origins), "Items": origins},
        "DefaultCacheBehavior": {
            "TargetOriginId": "relay", "ViewerProtocolPolicy": "https-only",
            "AllowedMethods": {"Quantity": 3, "Items": ["GET", "HEAD", "OPTIONS"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            "CachePolicyId": CACHING_DISABLED, "OriginRequestPolicyId": ALL_VIEWER_AND_CF_HEADERS,
            "Compress": False, "SmoothStreaming": False, "FieldLevelEncryptionId": "",
            "LambdaFunctionAssociations": {"Quantity": 0}, "FunctionAssociations": {"Quantity": 0},
            "TrustedSigners": {"Enabled": False, "Quantity": 0}, "TrustedKeyGroups": {"Enabled": False, "Quantity": 0}},
        "CacheBehaviors": behaviors,
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True, "MinimumProtocolVersion": "TLSv1.2_2021"},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "Logging": {"Enabled": False, "IncludeCookies": False, "Bucket": "", "Prefix": ""},
        "WebACLId": "", "CustomErrorResponses": {"Quantity": 0}}


def ensure_distribution(st, relay_host, secret, index_host=None, oac=None, cache_policy=None):
    cfg = distribution_config(st, relay_host, secret, index_host, oac, cache_policy)
    if st.get("cf_id"):
        cur = aws("cloudfront", "get-distribution-config", "--id", st["cf_id"])
        cfg["CallerReference"] = cur["DistributionConfig"]["CallerReference"]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
        aws("cloudfront", "update-distribution", "--id", st["cf_id"], "--if-match", cur["ETag"],
            "--distribution-config", f"file://{f.name}")
        os.unlink(f.name)
        return st["cf_id"], st["cf_domain"]
    st["cf_ref"] = cfg["CallerReference"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"DistributionConfig": cfg, "Tags": {"Items": [TAG]}}, f)
    d = aws("cloudfront", "create-distribution-with-tags", "--distribution-config-with-tags", f"file://{f.name}")
    os.unlink(f.name)
    st["cf_id"], st["cf_domain"] = d["Distribution"]["Id"], d["Distribution"]["DomainName"]
    st["cf_arn"] = d["Distribution"]["ARN"]
    save_state(st)
    say(f"created CloudFront {st['cf_id']} ({st['cf_domain']})")
    return st["cf_id"], st["cf_domain"]


def ensure_cache_policy():
    pols = aws("cloudfront", "list-cache-policies", "--type", "custom")["CachePolicyList"].get("Items", [])
    for p in pols:
        if p["CachePolicy"]["CachePolicyConfig"]["Name"] == "workstead-api":
            return p["CachePolicy"]["Id"]
    cfg = {"Name": "workstead-api", "Comment": "Honour the index's Cache-Control (tips: 30 s)", "DefaultTTL": 0,
           "MaxTTL": 60, "MinTTL": 0, "ParametersInCacheKeyAndForwardedToOrigin": {
               "EnableAcceptEncodingGzip": True, "EnableAcceptEncodingBrotli": True,
               "HeadersConfig": {"HeaderBehavior": "none"}, "CookiesConfig": {"CookieBehavior": "none"},
               "QueryStringsConfig": {"QueryStringBehavior": "all"}}}
    return aws("cloudfront", "create-cache-policy", "--cache-policy-config", json.dumps(cfg))["CachePolicy"]["Id"]


def ensure_oac():
    items = aws("cloudfront", "list-origin-access-controls")["OriginAccessControlList"].get("Items", [])
    for o in items:
        if o["Name"] == "workstead-index":
            return o["Id"]
    return aws("cloudfront", "create-origin-access-control", "--origin-access-control-config", json.dumps({
        "Name": "workstead-index", "Description": "CloudFront-only access to the index Lambda URL",
        "SigningProtocol": "sigv4", "SigningBehavior": "always",
        "OriginAccessControlOriginType": "lambda"}))["OriginAccessControl"]["Id"]


def wait_health(domain, path="/health", minutes=25):
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"https://{domain}{path}", timeout=10) as r:
                if r.status == 200:
                    return json.loads(r.read().decode() or "{}")
        except Exception:
            pass  # best effort: the connection or cache is optional here
        time.sleep(15)
    raise RuntimeError(f"https://{domain}{path} never became healthy")


# --- IAM, DynamoDB, Lambda ------------------------------------------------------------------


def ensure_role(name, service, policy):
    r = aws("iam", "get-role", "--role-name", name, check=False)
    if not r:
        r = aws("iam", "create-role", "--role-name", name, "--assume-role-policy-document", json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
                                                    "Action": "sts:AssumeRole"}]}),
                "--tags", "Key=project,Value=workstead")
        time.sleep(10)  # IAM propagation
    aws("iam", "put-role-policy", "--role-name", name, "--policy-name", "workstead", "--policy-document", json.dumps(policy))
    return r["Role"]["Arn"]


def ensure_table(name):
    if not aws("dynamodb", "describe-table", "--table-name", name, check=False):
        aws("dynamodb", "create-table", "--table-name", name, "--attribute-definitions",
            "AttributeName=pk,AttributeType=S", "AttributeName=sk,AttributeType=S",
            "--key-schema", "AttributeName=pk,KeyType=HASH", "AttributeName=sk,KeyType=RANGE",
            "--provisioned-throughput", "ReadCapacityUnits=5,WriteCapacityUnits=5",
            "--tags", "Key=project,Value=workstead")
        aws("dynamodb", "wait", "table-exists", "--table-name", name)
        aws("dynamodb", "update-time-to-live", "--table-name", name, "--time-to-live-specification",
            "Enabled=true,AttributeName=exp")
        say(f"created DynamoDB table {name} (provisioned 5/5: always free)")
    return aws("dynamodb", "describe-table", "--table-name", name)["Table"]["TableArn"]


def ensure_function(name, role, handler_file, env, memory=128, timeout=10):
    code = lambda_zip(handler_file)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(code)
    fn = aws("lambda", "get-function", "--function-name", name, check=False)
    vars_ = json.dumps({"Variables": env})
    if not fn:
        for attempt in range(6):
            try:
                aws("lambda", "create-function", "--function-name", name, "--runtime", "python3.12",
                    "--architectures", "arm64", "--role", role, "--handler", handler_file.replace(".py", ".handler"),
                    "--zip-file", f"fileb://{f.name}", "--memory-size", str(memory), "--timeout", str(timeout),
                    "--environment", vars_, "--tags", "project=workstead")
                break
            except RuntimeError as e:
                if "cannot be assumed" not in str(e) or attempt == 5:
                    raise
                time.sleep(10)
        aws("lambda", "wait", "function-active-v2", "--function-name", name)
    else:
        aws("lambda", "update-function-code", "--function-name", name, "--zip-file", f"fileb://{f.name}")
        aws("lambda", "wait", "function-updated-v2", "--function-name", name)
        aws("lambda", "update-function-configuration", "--function-name", name, "--environment", vars_,
            "--memory-size", str(memory), "--timeout", str(timeout))
        aws("lambda", "wait", "function-updated-v2", "--function-name", name)
    os.unlink(f.name)
    group = f"/aws/lambda/{name}"
    aws("logs", "create-log-group", "--log-group-name", group, "--tags", "project=workstead", check=False)
    aws("logs", "put-retention-policy", "--log-group-name", group, "--retention-in-days", "3")
    return aws("lambda", "get-function", "--function-name", name)["Configuration"]["FunctionArn"]


def ensure_index(region, account, bucket, st):
    table = "workstead-index"
    tarn = ensure_table(table)
    role = ensure_role("workstead-index-role", "lambda.amazonaws.com", {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
         "Resource": tarn},
        {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/art/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/workstead-index:*"}]})
    arn = ensure_function("workstead-index", role, "index.py", {"TABLE": table, "BUCKET": bucket})
    try:
        aws("lambda", "put-function-concurrency", "--function-name", "workstead-index",
            "--reserved-concurrent-executions", "5")
    except RuntimeError as e:
        say(f"WARNING: couldn't reserve concurrency (account limit?): {e}")
    url = aws("lambda", "get-function-url-config", "--function-name", "workstead-index", check=False)
    if not url:
        url = aws("lambda", "create-function-url-config", "--function-name", "workstead-index", "--auth-type", "AWS_IAM")
    host = url["FunctionUrl"].split("//", 1)[1].rstrip("/")
    return arn, host


def allow_cloudfront_invoke(dist_arn):
    for action, sid, extra in (("lambda:InvokeFunctionUrl", "cf-url", ["--function-url-auth-type", "AWS_IAM"]),
                               ("lambda:InvokeFunction", "cf-invoke", [])):
        aws("lambda", "add-permission", "--function-name", "workstead-index", "--statement-id", sid, "--action", action,
            "--principal", "cloudfront.amazonaws.com", "--source-arn", dist_arn, *extra, check=False)


# --- guardrails -------------------------------------------------------------------------------


def ensure_guardrails(region, account, st):
    topic = aws("sns", "create-topic", "--name", "workstead-brake", "--tags", "Key=project,Value=workstead")["TopicArn"]
    aws("sns", "set-topic-attributes", "--topic-arn", topic, "--attribute-name", "Policy", "--attribute-value", json.dumps({
        "Version": "2012-10-17", "Statement": [{"Sid": "budgets", "Effect": "Allow",
                                                "Principal": {"Service": "budgets.amazonaws.com"},
                                                "Action": "SNS:Publish", "Resource": topic,
                                                "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}))
    role = ensure_role("workstead-brake-role", "lambda.amazonaws.com", {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "lambda:PutFunctionConcurrency",
         "Resource": f"arn:aws:lambda:{region}:{account}:function:workstead-index"},
        {"Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*"},
        {"Effect": "Allow", "Action": "ec2:StopInstances", "Resource": "*",
         "Condition": {"StringEquals": {"aws:ResourceTag/project": "workstead"}}},
        {"Effect": "Allow", "Action": ["ce:UpdateCostAllocationTagsStatus", "ce:ListCostAllocationTags"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/workstead-brake:*"}]})
    arn = ensure_function("workstead-brake", role, "brake.py", {"INDEX_FUNCTION": "workstead-index"}, timeout=30)
    # Daily maintenance call: activates the cost allocation tag once billing has seen it.
    sched_role = ensure_role("workstead-scheduler-role", "scheduler.amazonaws.com", {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": arn}]})
    target = json.dumps({"Arn": arn, "RoleArn": sched_role, "Input": json.dumps({"maintenance": "activate-cost-tag"})})
    common = ["--name", "workstead-cost-tag", "--schedule-expression", "rate(1 day)",
              "--flexible-time-window", "Mode=OFF", "--target", target]
    if aws("scheduler", "get-schedule", "--name", "workstead-cost-tag", check=False):
        aws("scheduler", "update-schedule", *common)
    else:
        for attempt in range(5):
            try:
                aws("scheduler", "create-schedule", *common)
                break
            except RuntimeError as e:
                if attempt == 4:
                    raise
                time.sleep(8)  # IAM propagation for the new role
    aws("lambda", "add-permission", "--function-name", "workstead-brake", "--statement-id", "sns", "--action",
        "lambda:InvokeFunction", "--principal", "sns.amazonaws.com", "--source-arn", topic, check=False)
    subs = aws("sns", "list-subscriptions-by-topic", "--topic-arn", topic)["Subscriptions"]
    if not any(s["Endpoint"] == arn for s in subs):
        aws("sns", "subscribe", "--topic-arn", topic, "--protocol", "lambda", "--notification-endpoint", arn)
    # Budgets only see tagged spend once the cost allocation tag is active.
    r = aws("ce", "update-cost-allocation-tags-status", "--cost-allocation-tags-status",
            "TagKey=project,Status=Active", check=False)
    if r is None or r.get("Errors"):
        say("note: the 'project' cost allocation tag isn't active yet (AWS needs to see tagged usage first); "
            "the daily workstead-cost-tag schedule activates it automatically")
    budget = {"BudgetName": "workstead-monthly", "BudgetLimit": {"Amount": "10", "Unit": "USD"},
              "TimeUnit": "MONTHLY", "BudgetType": "COST",
              "CostFilters": {"TagKeyValue": ["user:project$workstead"]}}
    notes = []
    email = [{"SubscriptionType": "EMAIL", "Address": BUDGET_EMAIL}] if BUDGET_EMAIL else []
    for pct, extra in ((10, []), (50, []), (100, [{"SubscriptionType": "SNS", "Address": topic}])):
        subs_ = email + extra
        if subs_:
            notes.append({"Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN",
                                           "Threshold": pct, "ThresholdType": "PERCENTAGE"}, "Subscribers": subs_})
    existing = aws("budgets", "describe-budget", "--account-id", account, "--budget-name", "workstead-monthly", check=False)
    if existing:
        aws("budgets", "update-budget", "--account-id", account, "--new-budget", json.dumps(budget))
    else:
        aws("budgets", "create-budget", "--account-id", account, "--budget", json.dumps(budget),
            "--notifications-with-subscribers", json.dumps(notes))
        say("created budget workstead-monthly: alerts at $1, $5, $10; brake at $10")
    return topic


# --- commands -------------------------------------------------------------------------------


def write_directory(domain):
    d = {"v": 1, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "about": "Where Workstead agents find relays. Seeds are fallback once 5 community relays are healthy.",
         "seeds": {"milestone": [f"wss://{domain}"], "coordination": [f"wss://{domain}"],
                   "index": f"https://{domain}/api", "operator": "workstead (AWS, ap-southeast-2)"},
         "community": {"milestone": PUBLIC_RELAYS, "coordination": []}}
    p = ROOT / "docs" / "directory.json"
    p.write_text(json.dumps(d, indent=2) + "\n")
    say(f"wrote {p.relative_to(ROOT)} (commit it; GitHub Pages serves it)")


def up():
    region, account = ctx()
    st = load_state()
    bucket = ensure_bucket(region, account)
    sha, key, url = upload_code(bucket)
    vpc, subnet = ensure_network(region, bucket)
    sg = ensure_sg(region, vpc)
    secret = origin_secret()
    current = [i for i in instances() if any(t["Key"] == "code" and t["Value"] == sha[:16] for t in i.get("Tags", []))]
    old = [i for i in instances() if i not in current]
    if current:
        iid = current[0]["InstanceId"]
        if current[0]["State"]["Name"] == "stopped":
            aws("ec2", "start-instances", "--instance-ids", iid)
            aws("ec2", "wait", "instance-running", "--instance-ids", iid)
    else:
        allow_boot_egress(sg, region)
        iid = launch(subnet, sg, url, sha, secret)
    host = origin_host(ipv6_of(iid))
    _, index_host = ensure_index(region, account, bucket, st)
    oac, cpol = ensure_oac(), ensure_cache_policy()
    cf_id, domain = ensure_distribution(st, host, secret, index_host, oac, cpol)
    st.update(instance=iid, origin=host, bucket=bucket, sg=sg, subnet=subnet, vpc=vpc, code=sha[:16])
    save_state(st)
    allow_cloudfront_invoke(st.get("cf_arn") or f"arn:aws:cloudfront::{account}:distribution/{cf_id}")
    say(f"waiting for https://{domain}/health (CloudFront deploys take a few minutes)...")
    health = wait_health(domain)
    say(f"relay healthy through CloudFront: {health}")
    lockdown(iid, sg)
    for i in old:
        aws("ec2", "terminate-instances", "--instance-ids", i["InstanceId"])
        say(f"terminated previous instance {i['InstanceId']}")
    ensure_guardrails(region, account, st)
    write_directory(domain)
    say(f"done. relay: wss://{domain}  index: https://{domain}/api")


def status():
    ctx()
    st = load_state()
    print(json.dumps(st, indent=1))
    if st.get("cf_domain"):
        for path in ("/health", "/api/health"):
            try:
                with urllib.request.urlopen(f"https://{st['cf_domain']}{path}", timeout=10) as r:
                    print(path, r.status, r.read().decode()[:200])
            except Exception as e:
                print(path, "ERROR", e)


def brake():
    ctx()
    aws("lambda", "put-function-concurrency", "--function-name", "workstead-index", "--reserved-concurrent-executions", "0")
    ids = [i["InstanceId"] for i in instances() if i["State"]["Name"] in ("pending", "running")]
    if ids:
        aws("ec2", "stop-instances", "--instance-ids", *ids)
    say(f"brake applied: index throttled, stopped {ids}")


def resume():
    ctx()
    aws("lambda", "put-function-concurrency", "--function-name", "workstead-index", "--reserved-concurrent-executions", "5")
    ids = [i["InstanceId"] for i in instances() if i["State"]["Name"] == "stopped"]
    if ids:
        aws("ec2", "start-instances", "--instance-ids", *ids)
    say(f"resumed: index concurrency 5, started {ids}")


def down():
    """Delete every resource `up` created. Nothing else in the account is touched."""
    region, account = ctx()
    st = load_state()
    gone = lambda *a: aws(*a, check=False)  # noqa: E731  (already-deleted is fine)
    gone("budgets", "delete-budget", "--account-id", account, "--budget-name", "workstead-monthly")
    gone("scheduler", "delete-schedule", "--name", "workstead-cost-tag")
    topic = f"arn:aws:sns:{region}:{account}:workstead-brake"
    gone("sns", "delete-topic", "--topic-arn", topic)
    for fn in ("workstead-index", "workstead-brake"):
        gone("lambda", "delete-function", "--function-name", fn)
        gone("logs", "delete-log-group", "--log-group-name", f"/aws/lambda/{fn}")
    for role in ("workstead-index-role", "workstead-brake-role", "workstead-scheduler-role"):
        gone("iam", "delete-role-policy", "--role-name", role, "--policy-name", "workstead")
        gone("iam", "delete-role", "--role-name", role)
    gone("dynamodb", "delete-table", "--table-name", "workstead-index")
    gone("ssm", "delete-parameter", "--name", "/workstead/seed/origin-secret")
    ids = [i["InstanceId"] for i in instances() if i["State"]["Name"] != "terminated"]
    if ids:
        aws("ec2", "terminate-instances", "--instance-ids", *ids)
        say(f"terminating {ids}")
    cf = st.get("cf_id")
    if cf and gone("cloudfront", "get-distribution", "--id", cf):
        cur = aws("cloudfront", "get-distribution-config", "--id", cf)
        if cur["DistributionConfig"]["Enabled"]:
            cfg = cur["DistributionConfig"]
            cfg["Enabled"] = False
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(cfg, f)
            aws("cloudfront", "update-distribution", "--id", cf, "--if-match", cur["ETag"],
                "--distribution-config", f"file://{f.name}")
            os.unlink(f.name)
            say(f"disabled CloudFront {cf}; waiting for it to deploy (up to ~15 minutes)...")
        aws("cloudfront", "wait", "distribution-deployed", "--id", cf)
        etag = aws("cloudfront", "get-distribution", "--id", cf)["ETag"]
        aws("cloudfront", "delete-distribution", "--id", cf, "--if-match", etag)
        say(f"deleted CloudFront {cf}")
    for o in aws("cloudfront", "list-origin-access-controls")["OriginAccessControlList"].get("Items", []):
        if o["Name"] == "workstead-index":
            etag = aws("cloudfront", "get-origin-access-control", "--id", o["Id"])["ETag"]
            gone("cloudfront", "delete-origin-access-control", "--id", o["Id"], "--if-match", etag)
    for p in aws("cloudfront", "list-cache-policies", "--type", "custom")["CachePolicyList"].get("Items", []):
        if p["CachePolicy"]["CachePolicyConfig"]["Name"] == "workstead-api":
            pid = p["CachePolicy"]["Id"]
            etag = aws("cloudfront", "get-cache-policy", "--id", pid)["ETag"]
            gone("cloudfront", "delete-cache-policy", "--id", pid, "--if-match", etag)
    if ids:
        aws("ec2", "wait", "instance-terminated", "--instance-ids", *ids)
    tagf = ["Name=tag:project,Values=workstead", f"Name=tag:Name,Values={NAME}"]
    for vpc in [v["VpcId"] for v in find("vpcs", tagf)]:
        inv = [f"Name=vpc-id,Values={vpc}"]
        for ep in find("vpc-endpoints", inv):
            gone("ec2", "delete-vpc-endpoints", "--vpc-endpoint-ids", ep["VpcEndpointId"])
        for sg in find("security-groups", inv):
            if sg["GroupName"] != "default":
                gone("ec2", "delete-security-group", "--group-id", sg["GroupId"])
        for sn in find("subnets", inv):
            gone("ec2", "delete-subnet", "--subnet-id", sn["SubnetId"])
        for rt in find("route-tables", inv):
            if not any(a.get("Main") for a in rt.get("Associations", [])):
                gone("ec2", "delete-route-table", "--route-table-id", rt["RouteTableId"])
        for igw in find("internet-gateways", [f"Name=attachment.vpc-id,Values={vpc}"]):
            gone("ec2", "detach-internet-gateway", "--internet-gateway-id", igw["InternetGatewayId"], "--vpc-id", vpc)
            gone("ec2", "delete-internet-gateway", "--internet-gateway-id", igw["InternetGatewayId"])
        for _ in range(12):
            if gone("ec2", "delete-vpc", "--vpc-id", vpc) is not None:
                break
            time.sleep(10)  # endpoint deletion takes a moment
        say(f"deleted VPC {vpc}")
    bucket = f"workstead-artifacts-{account}-{region}"
    if gone("s3api", "head-bucket", "--bucket", bucket) is not None:
        subprocess.run(["aws", "s3", "rm", f"s3://{bucket}", "--recursive", "--quiet"], check=False)
        gone("s3api", "delete-bucket", "--bucket", bucket)
        say(f"deleted bucket {bucket}")
    if STATE.exists():
        STATE.unlink()
    left = aws("resourcegroupstaggingapi", "get-resources", "--tag-filters", "Key=project,Values=workstead")
    arns = [r["ResourceARN"] for r in left.get("ResourceTagMappingList", [])]
    say("down. nothing tagged project=workstead remains" if not arns else f"still listed (may lag): {arns}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"up": up, "status": status, "brake": brake, "resume": resume, "down": down}[cmd]()
