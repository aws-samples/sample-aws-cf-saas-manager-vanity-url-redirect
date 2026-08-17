# Changelog

All notable changes to this sample are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - Initial release

### Added
- **Phase 1 (`VanityRedirectCore`)** — DynamoDB redirect table, a Lambda@Edge
  `viewer-request` handler (`Host` → DynamoDB → `302`/`403` + HSTS), and a
  standard CloudFront distribution for testing on the default
  `*.cloudfront.net` domain (no custom domain required).
- **Phase 2 (`VanityRedirectSaaS`)** — a CloudFront **multi-tenant template
  distribution** (`connection_mode = tenant-only`), a **ConnectionGroup**, and
  one or more **DistributionTenants**, each with an auto-issued, auto-renewed
  **managed ACM certificate**. Reuses the Phase 1 Lambda@Edge function.
- Support for **multiple tenants** on one distribution via
  `-c tenant_subdomains=vanity,book` (each tenant → own cert → own redirect
  target).
- `scripts/seed_and_test.sh` for seeding DynamoDB and exercising the 302/403
  paths.
- Architecture sequence diagram (`docs/architecture-sequence.png`) and design
  notes (`docs/ARCHITECTURE.md`).
- Standard open-source repository files: `LICENSE` (MIT-0), `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, `NOTICE`.
- CI (`.github/workflows/ci.yml`): ruff lint, `cdk synth`, and gitleaks secret
  scanning.

### Security
- Open-redirect protection: only absolute `http(s)` targets with
  `status = active` are honored.
- HSTS header on all responses.
- Least-privilege IAM (`dynamodb:GetItem` on the redirect table only).
- No stored secrets.
