"""
Phase 2 SaaS Manager stack -- EXTERNAL DNS mode (your DNS provider stays authoritative).

No Route 53. Your DNS provider remains authoritative for your domain; you add a
single CNAME pointing the tenant FQDN at the CloudFront routing endpoint. This
requires no nameserver change and mirrors a real customer-owned-DNS model.
Undo = delete the CNAME.

New to CloudFront SaaS Manager terms (tenant-only, ConnectionGroup, routing
endpoint, DistributionTenant)? They are defined in the README's "Key concepts"
glossary.

Creates:
  1. A multi-tenant TEMPLATE distribution (connection_mode = tenant-only) reusing
     the core Lambda@Edge redirect function, with access logging and a viewer
     certificate pinned to TLSv1.2_2021.
  2. A ConnectionGroup -> routing endpoint (the CNAME target you add at your DNS provider).
  3. (gated) One DistributionTenant per subdomain with its own managed ACM cert.
     Only created with -c create_tenant=true, AFTER the CNAME resolves publicly.

Deploy order:
  (a) cdk deploy VanityRedirectSaaS -c enable_saas=true            # infra only
  (b) add CNAME at your DNS provider: <subdomain>.<domain> -> <RoutingEndpoint output>
  (c) cdk deploy VanityRedirectSaaS -c enable_saas=true -c create_tenant=true

Cost note: provisions billable resources (S3 logs bucket, CloudFront, ACM is free,
per-tenant resources). Run `cdk destroy` and empty/remove the logs bucket when done.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from cdk_nag import NagSuppressions
from constructs import Construct

# CloudFront ACM managed-cert enum: names which host serves the DNS-validation
# token. Not a secret. Kept as a module constant (name deliberately avoids the
# substring "token") so Bandit B106 does not false-positive on a string literal
# passed to the validation_token_host kwarg.
_CF_VALIDATION_HOST = "cloudfront"


class SaasStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str,
        edge_fn_arn: str,
        web_acl_arn: str,
        tenant_subdomains: list = None,
        create_tenant: bool = False,
        hardening: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        tenant_subdomains = tenant_subdomains or ["vanity"]
        _retain = RemovalPolicy.RETAIN if hardening else RemovalPolicy.DESTROY

        # Hardened S3 bucket for CloudFront access logs. CloudFront standard
        # logging requires SSE-S3 (SSE-KMS unsupported) and bucket-owner-preferred
        # object ownership (ACLs) for log delivery.
        log_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            object_lock_enabled=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            server_access_logs_prefix="s3-access/",
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=_retain,
        )
        # Enforce TLS in the canonical shape (Principal "*") so both Checkov and
        # cfn-guard's S3_BUCKET_SSL_REQUESTS_ONLY recognize it.
        log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowSSLRequestsOnly",
                effect=iam.Effect.DENY,
                principals=[iam.StarPrincipal()],
                actions=["s3:*"],
                resources=[log_bucket.bucket_arn, log_bucket.arn_for_objects("*")],
                conditions={"Bool": {"aws:SecureTransport": False}},
            )
        )

        # Viewer certificate for the template distribution, pinned to TLSv1.2_2021
        # (DNS-validated). Per-tenant DistributionTenants still get their own
        # managed ACM certs; this base cert lets the template enforce TLS>=1.2.
        certificate = acm.Certificate(
            self,
            "TemplateCertificate",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(),
        )

        # (1) Multi-tenant TEMPLATE distribution (tenant-only). Reuses the core
        # Lambda@Edge redirect function on viewer-request so tenants inherit the
        # exact redirect logic proven in Phase 1.
        template = cloudfront.CfnDistribution(
            self,
            "TemplateDistribution",
            distribution_config=cloudfront.CfnDistribution.DistributionConfigProperty(
                enabled=True,
                connection_mode="tenant-only",
                comment="Vanity-URL multi-tenant template",
                web_acl_id=web_acl_arn,
                default_cache_behavior=cloudfront.CfnDistribution.DefaultCacheBehaviorProperty(
                    target_origin_id="dummy",
                    viewer_protocol_policy="redirect-to-https",
                    cache_policy_id="4135ea2d-6df8-44a3-9df3-4b5a84be39ad",  # CachingDisabled
                    lambda_function_associations=[
                        cloudfront.CfnDistribution.LambdaFunctionAssociationProperty(
                            event_type="viewer-request",
                            lambda_function_arn=edge_fn_arn,
                        )
                    ],
                ),
                origins=[
                    cloudfront.CfnDistribution.OriginProperty(
                        id="dummy",
                        domain_name="example.com",
                        custom_origin_config=cloudfront.CfnDistribution.CustomOriginConfigProperty(
                            origin_protocol_policy="https-only",
                            origin_ssl_protocols=["TLSv1.2"],
                        ),
                    )
                ],
                tenant_config=cloudfront.CfnDistribution.TenantConfigProperty(
                    parameter_definitions=[]
                ),
                # Access logging (CKV_AWS_86) to the hardened bucket.
                logging=cloudfront.CfnDistribution.LoggingProperty(
                    bucket=log_bucket.bucket_regional_domain_name,
                    prefix="cloudfront/",
                    include_cookies=False,
                ),
                # Real ACM certificate + TLSv1.2_2021 (CKV_AWS_174); no default cert.
                viewer_certificate=cloudfront.CfnDistribution.ViewerCertificateProperty(
                    acm_certificate_arn=certificate.certificate_arn,
                    ssl_support_method="sni-only",
                    minimum_protocol_version="TLSv1.2_2021",
                ),
            ),
        )

        # (2) ConnectionGroup (no Anycast list -- only needed for apex domains,
        # and its quota is 0 in this account). Its routing endpoint is the CNAME
        # target you add at your DNS provider for the tenant subdomain.
        self.connection_group = cloudfront.CfnConnectionGroup(
            self,
            "ConnectionGroup",
            name="vanity-redirect-cg",
            enabled=True,
            ipv6_enabled=True,
        )

        # (3) One tenant per subdomain -- gated. Each DistributionTenant VERIFIES
        # domain ownership at create time, so each CNAME (sub -> routing
        # endpoint) must already resolve. Create with -c create_tenant=true.
        if create_tenant:
            for sub in tenant_subdomains:
                fqdn = f"{sub}.{domain_name}"
                tenant = cloudfront.CfnDistributionTenant(
                    self,
                    f"Tenant{sub.capitalize()}",
                    distribution_id=template.attr_id,
                    connection_group_id=self.connection_group.attr_id,
                    name=f"tenant-{sub}",
                    domains=[fqdn],
                    enabled=True,
                    # validation_token_host uses the _CF_VALIDATION_HOST constant
                    # (value "cloudfront") -- a required CloudFront ACM enum
                    # (which host serves the DNS-validation token), not a secret.
                    managed_certificate_request=cloudfront.CfnDistributionTenant.ManagedCertificateRequestProperty(
                        primary_domain_name=fqdn,
                        validation_token_host=_CF_VALIDATION_HOST,
                    ),
                )
                CfnOutput(self, f"TenantId{sub.capitalize()}", value=tenant.attr_id)

        CfnOutput(self, "TenantFqdns", value=",".join(f"{s}.{domain_name}" for s in tenant_subdomains))
        CfnOutput(self, "TemplateDistributionId", value=template.attr_id)
        CfnOutput(
            self,
            "RoutingEndpoint",
            value=self.connection_group.attr_routing_endpoint,
            description="ADD A CNAME (at your DNS provider) for EACH subdomain -> this value",
        )

        # --- CDK Nag: only genuinely inapplicable / opt-in items -----------------
        # Access logging (CFR3) and viewer TLS (CFR4) are now really implemented.
        NagSuppressions.add_resource_suppressions(
            template,
            [
                {
                    "id": "AwsSolutions-CFR1",
                    "reason": "Global vanity-URL redirect; geo restriction is not applicable.",
                },
                {
                    "id": "AwsSolutions-CFR5",
                    "reason": (
                        "The placeholder origin is never contacted (viewer-request "
                        "returns the 302 at the edge); origin protocol is https-only, "
                        "TLSv1.2."
                    ),
                },
            ],
        )
