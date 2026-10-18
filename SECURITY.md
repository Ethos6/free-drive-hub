# Security policy

## Supported versions

Security fixes are made on the latest release and the `main` branch.

| Version | Supported |
|---|---|
| 0.1.x (latest) | Yes |
| Older | No, please upgrade |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub: open the **Security** tab of this repository and click **Report a vulnerability**. Only the maintainer can see the report.

Please include:

- what an attacker could do, and which version or commit you tested
- steps to reproduce (a request, script or screenshot helps)
- any suggested fix

What to expect:

- an acknowledgement within 3 days
- an assessment and a plan within 10 days
- a fix and a published advisory crediting you (unless you prefer otherwise) once a patched version is released

## Scope

In scope: the Drive Hub server (`drivehub/`), its web app, WebDAV endpoint, login and two-factor sign-in, token storage, and the Docker image built from this repository.

Out of scope: problems in Google Drive itself, attacks that need access to the server's data folder or shell, and setups that ignore the README's advice (for example exposing the server over plain HTTP on the internet).

## Hardening tips for people running Drive Hub

- Serve it over HTTPS only and turn on two-factor sign-in.
- Use a separate, revocable app password for each WebDAV device.
- Back up and protect the `data/` folder: it holds the encrypted Google tokens and the key that decrypts them.
- Keep the container updated (`docker compose pull && docker compose up -d --build`).
