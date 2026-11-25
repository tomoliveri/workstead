"""The budget brake: when Workstead's AWS spend crosses the hard limit, stop it.

Subscribed to the workstead-brake SNS topic, which only the budget's top
threshold publishes to. It sets the index Lambda's concurrency to 0 (HTTP 429
for everyone) and stops the seed relay instance. Nothing is deleted:
`infra/aws/deploy.py resume` undoes it.

A daily EventBridge schedule also calls it in maintenance mode, which
activates the `project` cost allocation tag as soon as AWS allows (it must
first see tagged usage). Until then the tag-scoped budget can't see spend, so
this closes that gap without anyone having to remember.
"""
import os

import boto3


def handler(event, context):
    if isinstance(event, dict) and event.get("maintenance") == "activate-cost-tag":
        ce = boto3.client("ce")
        try:
            r = ce.update_cost_allocation_tags_status(CostAllocationTagsStatus=[{"TagKey": "project", "Status": "Active"}])
            errors = r.get("Errors", [])
        except Exception as e:  # not visible to billing yet: try again tomorrow
            errors = [str(e)]
        print(f"cost allocation tag 'project': {'active' if not errors else errors}")
        return {"activated": not errors}
    lam, ec2 = boto3.client("lambda"), boto3.client("ec2")
    lam.put_function_concurrency(FunctionName=os.environ["INDEX_FUNCTION"], ReservedConcurrentExecutions=0)
    ids = [i["InstanceId"] for r in ec2.describe_instances(Filters=[
        {"Name": "tag:project", "Values": ["workstead"]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]}])["Reservations"] for i in r["Instances"]]
    if ids:
        ec2.stop_instances(InstanceIds=ids)
    print(f"workstead brake applied: index concurrency 0, stopped {ids}")
    return {"stopped": ids}
