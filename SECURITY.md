# Security Policy

## Supported versions

Agent Decoder is in the 0.x series. Security fixes are applied to the latest minor version; older 0.x minors are not back-patched.

| Version       | Supported |
| ------------- | --------- |
| 0.x (latest)  | Yes       |
| 0.x (older)   | No        |

## Reporting a vulnerability

Please report vulnerabilities **privately**, not in a public issue.

Report it through GitHub's [private vulnerability reporting](https://github.com/Comradery64/Agent-Decoder/security/advisories/new) — the repository's **Security → Report a vulnerability** tab.

Include, at minimum:

- A description of the issue and its impact
- Steps to reproduce (a minimal proof-of-concept is ideal)
- Affected version(s) and platform
- Any relevant logs or screenshots

What to expect:

- **Acknowledgment within 72 hours** of your report.
- For critical issues, **a fix or mitigation targeted within 14 days**. Less severe issues get rolled into the next regular release.
- Coordinated disclosure — please give us a reasonable window to ship a fix before publishing details.

## Scope and threat model

Agent Decoder runs entirely on the user's machine and binds to `127.0.0.1` by default. There is no telemetry, no remote backend, and no authentication layer — the security model assumes the local user trusts processes running as themselves.

The interesting attack surface is therefore narrow:

- **`--expose` mode**, which binds to `0.0.0.0` and exposes the API to the local network. Anyone on that network can read chat history.
- **The file viewer**, which reads and (with `--enable-edit`) writes files on disk. Path traversal, symlink escapes, and writes outside the session-related sandbox are in scope.
- **JSONL parsing** of session files (malformed or maliciously crafted input).
- **The headless runner**, which invokes a subprocess CLI on the user's behalf.

Out of scope:

- Vulnerabilities that require an attacker already having local code execution as the same user.
- Issues only reachable when the user has explicitly enabled `--expose` on an untrusted network (this is documented as unsafe).

## Bug bounty

There is no monetary bounty program. Researchers who report valid vulnerabilities will be credited by name in `CHANGELOG.md` (unless they prefer to remain anonymous).
