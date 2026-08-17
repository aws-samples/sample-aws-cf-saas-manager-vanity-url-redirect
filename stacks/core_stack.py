"""
Phase 1 core stack -- Lambda@Edge redirect over a custom domain + ACM certificate.

CloudFront invokes the redirect logic NATIVELY at the edge on viewer-request
(no Function URL, no OAC, no origin auth) and returns the 302 directly. The
origin is never contacted.

The distribution is served over a customer-owned domain with an AWS-managed ACM
certificate pinned to a minimum of TLSv1.2_2021. Provide your domain with
`-c domain_name=example.com`; the ACM certificate is created with DNS validation,
so add the emitted CNAME at your DNS provider to complete issuance before the
stack finishes deploying.

Security posture (always on, so the sample passes security review as published):
  - DynamoDB encrypted with a customer-managed KMS key (automatic rotation).
  - DynamoDB point-in-time recovery enabled.
  - CloudFront access logging to a hardened, self-logging S3 bucket.
  - Viewer TLS minimum TLSv1.2_2021 via an ACM certificate (no default cert).
  - A WAFv2 WebACL (rate-based + AWS managed rules) attached to the distribution.

Opt-in extras:
  - `-c hardening=true`  -> deletion protection + RETAIN on the table & KMS key.

Cost note: this stack provisions billable resources (a KMS customer-managed key,
an S3 logs bucket, a WAFv2 WebACL, CloudFront, Lambda@Edge, and DynamoDB). It is
NOT free-tier only. Run `cdk destroy` and empty/remove the logs bucket when done.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_wafv2 as wafv2
from cdk_nag import NagSuppressions
from constructs import Construct

# Lambda@Edge has no env vars, so the table name is a fixed constant shared
# between the stack and the edge handler.
TABLE_NAME = "vanity-redirect-sample"


def _cfn_function(construct) -> lambda_.CfnFunction:
    """Return the first AWS::Lambda::Function CfnResource under a construct."""
    for child in construct.node.find_all():
        if isinstance(child, lambda_.CfnFunction):
            return child
    raise ValueError("No CfnFunction found under construct")


class CoreStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str,
        hardening: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        _retain = RemovalPolicy.RETAIN if hardening else RemovalPolicy.DESTROY

        # (1) Redirect map -- DynamoDB with a customer-managed KMS CMK (rotation)
        # and point-in-time recovery. Deletion protection + RETAIN are opt-in via
        # -c hardening=true.
        self.table_key = kms.Key(
            self,
            "RedirectTableKey",
            description="CMK for the vanity-redirect DynamoDB table",
            enable_key_rotation=True,
            removal_policy=_retain,
        )
        self.table = dynamodb.Table(
            self,
            "RedirectTable",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="host", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.table_key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            deletion_protection=hardening,
            removal_policy=_retain,
        )

        # (2) Lambda@Edge function (must live in us-east-1; replicated to edges).
        self.edge_fn = cloudfront.experimental.EdgeFunction(
            self,
            "RedirectEdgeFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambda/redirect_edge"),
        )
        # Edge function's execution role needs read access to the table (and, via
        # grant_read_data, kms:Decrypt on the table CMK).
        self.table.grant_read_data(self.edge_fn)

        # (3) Hardened S3 bucket for CloudFront access logs.
        #   - CloudFront standard logging requires SSE-S3 (SSE-KMS is unsupported)
        #     and bucket-owner-preferred object ownership (ACLs) for log delivery.
        #   - The bucket logs its own access to itself (server access logs) and
        #     enforces TLS; public access is fully blocked and objects expire.
        self.log_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            object_lock_enabled=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            server_access_logs_prefix="s3-access/",
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=RemovalPolicy.RETAIN if hardening else RemovalPolicy.DESTROY,
        )
        # Enforce TLS in the canonical shape (Principal "*") so both Checkov and
        # cfn-guard's S3_BUCKET_SSL_REQUESTS_ONLY recognize it. CDK's enforce_ssl
        # emits Principal {"AWS":"*"}, which cfn-guard does not match.
        self.log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowSSLRequestsOnly",
                effect=iam.Effect.DENY,
                principals=[iam.StarPrincipal()],
                actions=["s3:*"],
                resources=[
                    self.log_bucket.bucket_arn,
                    self.log_bucket.arn_for_objects("*"),
                ],
                conditions={"Bool": {"aws:SecureTransport": False}},
            )
        )

        # (4) ACM certificate (DNS-validated) for the viewer domain. Pinned to
        # TLSv1.2_2021 on the distribution -- no default CloudFront certificate.
        self.certificate = acm.Certificate(
            self,
            "ViewerCertificate",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(),
        )

        # A distribution needs an origin even though viewer-request short-circuits
        # before any origin fetch. Use a harmless placeholder origin.
        dummy_origin = origins.HttpOrigin("example.com")

        # WAFv2 WebACL (always on): rate-based throttling + AWS managed common
        # rules to blunt EDoS / volumetric abuse on the public, cache-disabled
        # endpoint. CLOUDFRONT-scoped WebACLs must be created in us-east-1. The
        # same WebACL ARN is shared with the Phase 2 template distribution.
        web_acl = wafv2.CfnWebACL(
            self,
            "RedirectWebAcl",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="vanityRedirectWebAcl",
                sampled_requests_enabled=True,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitPerIP",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=2000,
                            aggregate_key_type="IP",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="rateLimitPerIp",
                        sampled_requests_enabled=True,
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSCommonRules",
                    priority=1,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesCommonRuleSet",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="awsCommonRules",
                        sampled_requests_enabled=True,
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSKnownBadInputs",
                    priority=2,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesKnownBadInputsRuleSet",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="awsKnownBadInputs",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )
        self.web_acl_arn = web_acl.attr_arn
        web_acl_id = web_acl.attr_arn

        self.distribution = cloudfront.Distribution(
            self,
            "RedirectDist",
            default_behavior=cloudfront.BehaviorOptions(
                origin=dummy_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                edge_lambdas=[
                    cloudfront.EdgeLambda(
                        function_version=self.edge_fn.current_version,
                        event_type=cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            domain_names=[domain_name],
            certificate=self.certificate,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            ssl_support_method=cloudfront.SSLMethod.SNI,
            enable_logging=True,
            log_bucket=self.log_bucket,
            log_file_prefix="cloudfront/",
            web_acl_id=web_acl_id,
            comment="Vanity-URL redirect sample (Phase 1 - Lambda@Edge)",
        )

        CfnOutput(self, "TableName", value=self.table.table_name)
        CfnOutput(
            self,
            "DistributionDomain",
            value=self.distribution.distribution_domain_name,
            description="CloudFront domain; point your domain's DNS at it.",
        )

        # --- CDK Nag: only genuinely inapplicable items -------------------------
        # Access logging (CFR3), viewer TLS (CFR4), and WAF (CFR2) are now really
        # implemented, so they are no longer suppressed.
        NagSuppressions.add_resource_suppressions(
            self.distribution,
            [
                {
                    "id": "AwsSolutions-CFR1",
                    "reason": "Global vanity-URL redirect; geo restriction is not applicable.",
                },
            ],
        )
        NagSuppressions.add_resource_suppressions(
            self.edge_fn,
            [
                {
                    "id": "AwsSolutions-L1",
                    "reason": (
                        "Runtime pinned to Python 3.12, a tested Lambda@Edge-supported "
                        "version; edge runtime bumps are validated before rollout."
                    ),
                },
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWSLambdaBasicExecutionRole is attached by the CDK EdgeFunction "
                        "construct for CloudWatch Logs write access only."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                },
            ],
            apply_to_children=True,
        )

        # --- Lambda@Edge platform limitations (technically inapplicable) --------
        # Edge functions CANNOT use a VPC, a dead-letter queue, or reserved
        # concurrency -- AWS rejects those on Lambda@Edge. These checks are not
        # accepted risks; they are impossible to satisfy for this compute type,
        # so they are marked N/A for both Checkov and cfn-guard. This is the only
        # place the code cannot make the scanner pass.
        _edge_cfn = _cfn_function(self.edge_fn)
        _edge_cfn.add_metadata(
            "checkov",
            {
                "skip": [
                    {"id": "CKV_AWS_115", "comment": "Lambda@Edge does not support reserved concurrency."},
                    {"id": "CKV_AWS_116", "comment": "Lambda@Edge does not support dead-letter queues."},
                    {"id": "CKV_AWS_117", "comment": "Lambda@Edge cannot run inside a VPC."},
                ]
            },
        )
        _edge_cfn.add_metadata(
            "guard",
            {"SuppressedRules": ["LAMBDA_DLQ_CHECK", "LAMBDA_INSIDE_VPC", "LAMBDA_CONCURRENCY_CHECK"]},
        )
