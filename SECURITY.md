# Security Policy

## Supported versions

Only the latest release of disko receives security fixes.

| Version | Supported |
| ------- | --------- |
| latest  | Yes       |
| older   | No        |

## Reporting a vulnerability

Please report security vulnerabilities by opening a GitHub issue in this
repository and applying the **"security"** label.

You can expect an initial response within **7 days**. If the issue is confirmed,
a fix will be prioritized and a patched release will be published as soon as
reasonably possible. You will be kept informed of progress via the issue thread.

Please do not include sensitive exploit details in a public issue. If the
vulnerability requires confidential disclosure, mention that in the issue and
we will arrange a private channel.

## Security model

disko is designed with the following security properties:

- **Localhost only** — the embedded HTTP server binds exclusively to `127.0.0.1`
  and is not accessible from other machines on the network.
- **No sensitive file contents** — the persistent cache stores only directory
  paths and size metadata. File contents are never read or stored.
- **No outbound requests** — disko makes no outbound network requests except to
  load the D3.js library from its CDN (used for treemap rendering in the browser).
  All scanning and serving happen locally.
- **No user code execution** — disko does not evaluate, import, or execute any
  code found on the scanned filesystem. It only reads directory entry metadata
  (names and sizes).
