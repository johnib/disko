# Security Policy

## Supported versions

Only the latest release of disko receives security fixes.

| Version | Supported |
| ------- | --------- |
| latest  | Yes       |
| older   | No        |

## Reporting a vulnerability

Please report security vulnerabilities privately using GitHub's
[Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
go to the repository's **Security** tab and click **Report a vulnerability**.
Please do **not** open a public issue for security problems.

You can expect an initial response within **7 days**. If the issue is confirmed,
a fix will be prioritized and a patched release will be published as soon as
reasonably possible. You will be kept informed of progress via the private
advisory thread.

## Security model

disko is designed with the following security properties:

- **Localhost only** — the embedded HTTP server binds exclusively to `127.0.0.1`
  and is not accessible from other machines on the network.
- **Protected against malicious websites** — a web page open in your browser
  can reach `localhost`, so the server treats the browser as part of the threat
  model. It sends no CORS headers (the UI is same-origin), rejects requests whose
  `Host` header is not `localhost`/`127.0.0.1` on disko's port (defeating DNS
  rebinding), and rejects cross-site requests based on the `Origin` and
  `Sec-Fetch-Site` headers (preventing CSRF-style scans or cache clearing and
  cross-origin reads of directory listings). Such requests get `403 Forbidden`.
  The only state-changing endpoint (`/invalidate`, which drops a cache entry)
  accepts `POST` only, so it can't be triggered by a plain link or `<img>` tag.
- **No sensitive file contents** — the persistent cache stores only directory
  paths and size metadata. File contents are never read or stored.
- **No outbound requests** — disko makes no outbound network requests except to
  load the D3.js library from its CDN (used for treemap rendering in the browser).
  All scanning and serving happen locally.
- **No user code execution** — disko does not evaluate, import, or execute any
  code found on the scanned filesystem. It only reads directory entry metadata
  (names and sizes).
