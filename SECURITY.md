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

Only the latest release, and `main`, get security fixes. There are no
maintained release branches: a fix lands on `main` and ships in the next
release and the `latest` image. Upgrade with `docker compose pull && docker
compose up -d`.

## What is in scope

- Ways to read clips, stills, descriptions or credentials **without network
  access to the UI's port**, such as path traversal through `/media/` or the
  API, or leaks into logs or notifications
- Ways for another web page to read from or act on Blynky through the owner's
  browser, such as DNS rebinding or cross-site requests getting past the
  Host, Origin and content-type checks
- Ways to read or delete files outside the archive through the API
- Anything that exposes `.blink_creds.json` or the Blink session token
- With the login on: any way past it, such as reaching a page, clip or API
  call without a session, forging a session, or reading
  `data/.session_secret`
- Injection through camera names, descriptions or other values that reach the
  page or the filesystem
- Docker image issues, such as running as root or baking secrets into a layer

## What is not a vulnerability

- **The UI has no login unless one is set.** The login (`AUTH_USER` and
  `AUTH_PASSWORD`) is optional and off by default, as
  [documented](README.md#security): Blynky binds `127.0.0.1` by default and is
  meant to sit behind something that controls access, such as
  `tailscale serve`. With the login off, "anyone who can reach the port can
  see the cameras" is expected behaviour, not a finding. So is the login's
  password and cookie being readable over plain HTTP, which the docs warn
  about.
- `.blink_creds.json` storing the Blink password in plain text. blinkpy stores
  it that way. Blynky makes the file readable only by its owner (`chmod 600`)
  and excludes it from git and Docker builds.
- Frames, descriptions and summaries going to Ollama's or Google's servers
  when a `:cloud` Ollama model or Gemini is chosen. That is what those
  options are, and the docs say so. The default model runs locally.
- Problems in blinkpy, Ollama or ntfy themselves. Report those upstream. Do
  report how Blynky uses them, though.

Please never include real camera footage, stills or credentials in a report.
