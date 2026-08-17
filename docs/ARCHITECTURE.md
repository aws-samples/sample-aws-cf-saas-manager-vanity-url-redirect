# Architecture

A multi-tenant vanity-URL redirect service on **Amazon CloudFront SaaS Manager**.
Many customer-owned custom domains each get their own auto-managed TLS
certificate and redirect target, served from **one** CloudFront multi-tenant
distribution — no ALB, no NLB, no origin server.

> **New to these terms?** This document uses AWS networking and CloudFront
> concepts (SNI, Lambda@Edge, ConnectionGroup, CNAME/A records, apex domains,
> Anycast). Each is defined in plain language in the
> [Key concepts glossary](../README.md#key-concepts), and the README links to
> AWS getting-started guides for the CLI, CloudFront, and DNS.

![Reference architecture](reference-architecture.png)

![Sequence](architecture-sequence.png)

## Why this pattern

An ALB-based redirect tier terminates TLS on a listener that holds at most
**100 SSL certificates** — a hard limit. Because customer domains differ,
wildcard certificates don't apply, so the certificate count equals the number of
custom domains and the design stops at about 100 customers.

CloudFront SaaS Manager removes that ceiling. There is no per-account quota on
SNI certificates, the default is 10,000 distribution tenants per account
(adjustable), and each tenant's certificate is issued and renewed for you, so
there is no certificate to attach or rotate by hand. These figures come from the
[CloudFront quotas](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html)
page.

## Components

| Component | Role |
|-----------|------|
| **DynamoDB table** | `host` (PK) → `{ targetUrl, status }`. The redirect map. Encrypted with a **customer-managed KMS key**; **point-in-time recovery** enabled. |
| **AWS KMS key** (CMK) | Customer-managed key with rotation enabled, encrypting the DynamoDB table at rest. |
| **Lambda@Edge** (`viewer-request`) | Reads `Host`, looks it up in DynamoDB, returns `302` (+ `Location`, HSTS) or `403`. Runs at the edge; the origin is never contacted. |
| **CloudFront multi-tenant distribution** (template, `connection_mode = tenant-only`) | Shared configuration all tenants inherit (viewer-request → Lambda@Edge). **WAF-protected**, TLS ≥ 1.2, access logging enabled. |
| **AWS WAF WebACL** | Rate-based rule + AWS Managed **Common** and **Known-Bad-Inputs** rule sets, attached to the distribution(s) at the edge. |
| **ACM certificate** (viewer) | Explicit ACM certificate pinned to **TLSv1.2_2021** for the distribution viewer, alongside the per-tenant managed certs. |
| **S3 access-logs bucket** | Hardened bucket (SSE-S3, block-public, versioned, Object Lock, TLS-only policy) receiving CloudFront access logs. |
| **ConnectionGroup** | Exposes the **routing endpoint** that tenant subdomains `CNAME` to. |
| **DistributionTenant** (one per domain) | Binds a custom domain + its **managed ACM certificate** to the template. |
| **Anycast static IP list** (apex only) | Fixed IPs for apex/naked domains that cannot use a `CNAME`. Quota-gated. |

Only the **tenant**, its **certificate**, and its **DynamoDB row** are
per-domain. Everything else (distribution, WAF, KMS key, logs bucket, edge
function, table) is shared.

## Request flow

1. User requests `https://vanity.example.com/`.
2. Customer DNS resolves the host to CloudFront:
   - **Subdomain** → `CNAME` → ConnectionGroup routing endpoint.
   - **Apex** → `A` record → Anycast static IPs.
3. **AWS WAF** evaluates the request at the edge (rate-based + AWS managed rules).
4. CloudFront selects the matching certificate via **SNI** (the per-tenant
   managed cert, or the distribution's ACM viewer cert) and terminates TLS at a
   minimum of **TLSv1.2_2021**.
5. On `viewer-request`, **Lambda@Edge** reads `Host`, does a DynamoDB `GetItem`,
   and returns `302` with the stored `Location` (or `403`). CloudFront writes an
   **access log** entry to the S3 logs bucket.
6. The browser follows the `302` to the target URL.

## Two-phase deployment (and why order matters)

Phase 1 stands up the core distribution — DynamoDB (KMS-encrypted, PITR),
Lambda@Edge, the WAF WebACL, the S3 access-logs bucket, and an **ACM viewer
certificate** for your domain. Because that certificate is DNS-validated,
deployment waits until you add the validation `CNAME`. Phase 2 then adds the
multi-tenant template and creates tenants with their own managed certificates. A
`DistributionTenant` verifies domain ownership when it is created, so the DNS
record has to resolve first. That is why Phase 2 is deployed in order: create
the infrastructure, add the DNS record, and only then create the tenant.

With `managedCertificateRequest` `validation_token_host = "cloudfront"`, the
single routing `CNAME` does double duty — routing and certificate ownership
validation — so there is no separate `_token` validation record to add.

## Subdomain vs apex

| Aspect | Subdomain (`vanity.example.com`) | Apex (`example.com`) |
|--------|----------------------------------|----------------------|
| DNS record | `CNAME` → routing endpoint | `A` → Anycast static IPs |
| Extra AWS resource | none | Anycast IP list + ConnectionGroup binding |
| Quota note | works by default | Anycast IP list quota (`L-6A19EDFD`) defaults to **0** — request an increase |

## Compute choice: Lambda@Edge vs Lambda origin

This sample uses **Lambda@Edge** (CloudFront invokes it natively on
`viewer-request`; no origin fetch, no public endpoint). An alternative is a
regional Lambda behind a Function URL used as a CloudFront origin — but public
and OAC-signed Function URLs may be blocked by organization guardrails (SCPs),
which is why the native Lambda@Edge invocation is the default here.

For lower edge latency at scale, back the lookup with a **DynamoDB global table**
so replicated edge functions read from a nearby Region.

## Security considerations

A redirect service can become an open-redirect vector, so the handler only
honors absolute `http(s)` targets whose row has `status = active`; for
production, add a per-tenant allowlist of permitted destination hosts. Every
response carries an HSTS header, and you can add your domains to the HSTS preload
list. The Lambda@Edge execution role is scoped to `dynamodb:GetItem` on the one
table and nothing more. Finally, request certificates only for domains you
control — ownership is verified through the DNS record you add.

Beyond the handler, the sample enforces defense-in-depth in the infrastructure:
an **AWS WAF** WebACL (rate-based + AWS managed Common and Known-Bad-Inputs rule
sets) fronts the distribution; the viewer certificate pins **TLSv1.2_2021**; the
DynamoDB table is encrypted with a **customer-managed KMS key** and has
**point-in-time recovery** enabled; and CloudFront **access logs** are written to
a hardened, TLS-only, versioned S3 bucket with Object Lock.

## Cost model (shape)

SaaS Manager bills per distribution tenant resource (confirmed in the CloudFront
FAQ; check the CloudFront pricing page or the AWS Price List API for the current
rate). On top of that you pay standard CloudFront request and data-transfer
charges, which are shared across tenants and small for tiny `302` responses, plus
per-invocation Lambda@Edge and per-request DynamoDB charges. The managed ACM
certificates carry no charge, but the **WAF WebACL**, the **KMS customer-managed
key**, and the **S3 access-logs bucket** are additional billable resources — this
sample is not free-tier only.
