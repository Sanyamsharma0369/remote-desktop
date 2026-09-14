# Security Policy

## Supported Versions

Security fixes are provided for the latest version on the `main` branch.

| Version | Supported          |
| ------- | ------------------ |
| 2.0.x   | :white_check_mark: |
| < 2.0   | :x:                |

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues, pull requests, or discussions.

Instead, please report security issues via:
- **GitHub Private Security Advisory:** Open an advisory under the Security tab of the repository.
- **Security Contact:** Email the repository maintainers privately.

Please include the following details in your report:
- Type of vulnerability (e.g. Authentication Bypass, Input Injection, CORS Misconfiguration, Memory Exhaustion).
- Step-by-step instructions to reproduce the issue.
- Proof of Concept (PoC) or sample payload if applicable.
- Estimated impact on the host system or network.
- Recommended mitigation if available.

> [!CAUTION]
> Do **NOT** attach real session tokens, passwords, `.env` files, production database dumps, or private screenshots in vulnerability reports.

## Security Scope

This project is a high-privilege remote administration tool. Security-sensitive surfaces include:
- Authentication, refresh-token rotation, and JWT validation.
- WebSocket single-use authorization tickets.
- Screen Control mode authorization and state transitions.
- Mouse coordinate clamping and keyboard input injection sanitization.
- REST API rate limiting and security headers (CSP, HSTS, X-Frame-Options).
- File upload MIME validation, directory traversal prevention, and filename sanitization.
- Power control confirmation and REST heartbeat verification.
- Reverse proxy TLS termination and CORS origin boundaries.
