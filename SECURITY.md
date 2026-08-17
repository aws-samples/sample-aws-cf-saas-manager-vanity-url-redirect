# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, we ask that you
notify AWS/Amazon Security via our
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/)
or directly via email to aws-security@amazon.com.

Please do **not** create a public GitHub issue for security vulnerabilities.

## Security Notes for This Sample

This is sample code intended for learning and demonstration. Before adapting it
for production, review the following:

- **Open-redirect protection** — the redirect handler only honors absolute
  `http(s)` targets and requires `status = active` in DynamoDB. Extend this with
  a per-tenant allowlist of permitted destination hosts before production use.
- **HSTS** — responses include `Strict-Transport-Security`. Consider HSTS
  preload for your domains.
- **Least privilege** — the Lambda@Edge execution role is granted only
  `dynamodb:GetItem` on the redirect table.
- **No secrets** — this sample stores no credentials. Redirect mappings in
  DynamoDB are non-sensitive host→URL pairs.
- **DNS validation** — per-tenant ACM certificates are issued for domains you
  control; never request certificates for domains you do not own.
