#!/usr/bin/env python3
"""
CDK app for the vanity-URL redirect sample.

A domain you control is required (both stacks attach a DNS-validated ACM
certificate). Deploy the core distribution:
    cdk deploy VanityRedirectCore -c domain_name=example.com

Add the multi-tenant SaaS layer:
    cdk deploy VanityRedirectSaaS \
        -c enable_saas=true \
        -c domain_name=example.com \
        -c tenant_subdomains=vanity

`-c key=value` passes an AWS CDK *context* value into the app (see the AWS CDK
docs). New to the CDK CLI or the terms below? See the README's "Key concepts"
glossary and the AWS CDK getting-started guide. The SaaS stack is only
synthesized when -c enable_saas=true, so the core stack works standalone; it
reuses the Lambda@Edge redirect function and WAF WebACL from the core stack.
"""

import aws_cdk as cdk
from aws_cdk import Aspects
from cdk_nag import AwsSolutionsChecks

from stacks.core_stack import CoreStack
from stacks.saas_stack import SaasStack

app = cdk.App()

# Security linting (CDK Nag): fail `cdk synth` on AWS Solutions Pack violations
# so the same gate runs locally and in CI. Accepted risks are documented with
# targeted NagSuppressions inside the individual stacks.
Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

# us-east-1 is required for CloudFront certs / SaaS Manager / Lambda@Edge.
env = cdk.Environment(region="us-east-1")

# Your registered domain for the viewer certificate (required). The ACM cert is
# DNS-validated, so add the emitted CNAME at your DNS provider to complete issuance.
domain_name = app.node.try_get_context("domain_name") or "example.com"

# Opt-in production hardening:
#   -c hardening=true   -> deletion protection + RETAIN on the table & KMS key (T5)
# A WAFv2 WebACL is always attached (rate-based + AWS managed rules).
_hardening = app.node.try_get_context("hardening") in ("true", "1", True)

core = CoreStack(
    app,
    "VanityRedirectCore",
    domain_name=domain_name,
    hardening=_hardening,
    env=env,
)

if app.node.try_get_context("enable_saas") in ("true", "1", True):
    # comma-separated list, e.g. -c tenant_subdomains=vanity,book
    subs_ctx = app.node.try_get_context("tenant_subdomains") or "vanity"
    tenant_subdomains = [s.strip() for s in subs_ctx.split(",") if s.strip()]
    SaasStack(
        app,
        "VanityRedirectSaaS",
        domain_name=domain_name,
        tenant_subdomains=tenant_subdomains,
        edge_fn_arn=core.edge_fn.edge_arn,
        web_acl_arn=core.web_acl_arn,
        create_tenant=app.node.try_get_context("create_tenant") in ("true", "1", True),
        hardening=_hardening,
        env=env,
    )

app.synth()
