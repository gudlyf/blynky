# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through
[GitHub's private vulnerability reporting](https://github.com/gudlyf/blynky/security/advisories/new)
(the **Security** tab → **Report a vulnerability**). Include what you found,
how to reproduce it, and what an attacker could do with it.

You should hear back within a week. Once a fix is ready, the advisory is
published with credit to you, unless you would rather stay anonymous.

## Supported versions

Only the latest commit on `main` is supported. There are no maintained
release branches.

## What is in scope

- Ways to read clips, stills, descriptions or credentials **without network
  access to the UI's port**, such as path traversal through `/media/` or the
  API, or leaks into logs or notifications
- Anything that exposes `.blink_creds.json` or the Blink session token
- Injection through camera names, descriptions or other values that reach the
  page or the filesystem
- Docker image issues, such as running as root or baking secrets into a layer

## What is not a vulnerability

- **The UI has no login.** This is by design and
  [documented](README.md#security): Blynky binds `127.0.0.1` by default and is
  meant to sit behind something that controls access, such as
  `tailscale serve`. "Anyone who can reach the port can see the cameras" is
  expected behaviour, not a finding.
- `.blink_creds.json` storing the Blink password in plain text. blinkpy stores
  it that way. Blynky makes the file readable only by its owner (`chmod 600`)
  and excludes it from git and Docker builds.
- Problems in blinkpy, Ollama or ntfy themselves. Report those upstream. Do
  report how Blynky uses them, though.

Please never include real camera footage, stills or credentials in a report.
