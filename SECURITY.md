# Security

If you find a vulnerability in Clawford — in `deploy.py`, the shared
libraries, the telegram relay, or anywhere else — please report it
privately rather than opening a public issue.

**Contact:** file a GitHub security advisory on this repo, or email
the maintainer directly (address in the repo profile).

The fleet's threat model — operator-as-admin, per-agent isolation,
immutable identity files, safeguards around `deploy.py`, and the
script contract that fences cron output — is documented in
[`guide-v3/19-security-and-hardening.md`](guide-v3/19-security-and-hardening.md).
Start there if you want context before reporting.

This is a personal project. There is no SLA on response time and no
bug bounty. Reports are appreciated nonetheless.
