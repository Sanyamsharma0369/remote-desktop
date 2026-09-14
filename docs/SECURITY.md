# Security Architecture & Hardening Guide

Remote desktop software operates with high privileges over the host system. This document outlines the defense-in-depth measures implemented in the application.

---

## 1. Threat Model & Mitigations

| Threat Vector | Mitigation Strategy |
|---|---|
| **Brute-Force Authentication** | SlowAPI rate limiting on `/api/auth/login` (5 attempts/min) + bcrypt hashing (work factor 12). |
| **Token Theft / Replay** | Short-lived access tokens (15m) + opaque refresh tokens rotated on every use with reuse detection. |
| **Cross-Site Scripting (XSS)** | Refresh tokens stored exclusively in `HttpOnly` cookies; strict Content-Security-Policy (CSP) applied to all HTML pages. |
| **Cross-Site Request Forgery (CSRF)** | `SameSite=Lax` / `Strict` cookie attributes; custom `Authorization: Bearer` header required for API actions. |
| **Unauthorized Input Injection** | Server-side `Screen View` default; WebSocket commands checked against active session mode. |
| **Malicious Key Combos** | Server-side keyboard sanitizer blocks dangerous OS shortcuts (`Win+R`, `Ctrl+Alt+Del`, system combos). |
| **Unbounded Mouse Events** | Rate-limiting on control messages (60 mouse/sec, 120 control/sec) and coordinate clamping. |
| **Dangerous File Uploads** | Allowed extensions whitelist, file size caps, path traversal checks, and sanitized filenames. |
| **Accidental System Power Actions** | Destructive actions (`Sleep`, `Restart`, `Shutdown`) require confirmation dialogs and REST API heartbeat verification. |

---

## 2. Token Rotation & Theft Detection

The system implements strict refresh token rotation with theft detection:

```text
Client                          Server                           Database
  │                               │                                 │
  ├────── POST /api/auth/refresh ─►                                 │
  │       (Token A in Cookie)     ├───── Verify Token A Hash ───────►
  │                               │                                 │
  │                               ├─── If Hash Matches:             │
  │                               │    1. Mark Token A Revoked ─────►
  │                               │    2. Issue Token B & Hash ─────►
  │                               │                                 │
  │                               ├─── If Token A Already Revoked:  │
  │                               │    (Possible Stolen Token Reused!)
  │                               │    1. Revoke ALL User Sessions ─►
  │                               │    2. Return 401 Unauthorized ──►
  ◄────── Response (New Cookie) ──┤                                 │
```

---

## 3. WebSocket Ticket Protocol

WebSockets cannot send custom HTTP authorization headers in standard browser APIs. To avoid placing long-lived JWTs in URL query parameters, we use short-lived, single-use tickets:

1. Client requests a ticket via authenticated REST API:
   `POST /api/auth/ws-ticket` with `Authorization: Bearer <jwt>`.
2. Server generates a cryptographically random ticket (60-second TTL) stored in SQLite.
3. Client opens WebSocket connection: `wss://.../ws/control?ticket=<ticket>`.
4. Server validates ticket, consumes it (single-use deletion), and authorizes the socket.

---

## 4. Input Sanitization & Clamping

- **Coordinate Clamping:** All mouse coordinates are normalized floats $[0.0, 1.0]$. The server enforces bounds checking to prevent out-of-screen injection or negative offsets.
- **Shortcut Sanitizer:**
  - `Win` key combinations (such as `Win+R`, `Win+X`) are blocked by default.
  - `Ctrl+Alt+Del` cannot be injected programmatically.
  - Dangerous window-closing shortcuts (e.g. `Alt+F4`) are configurable via `.env` (`ALLOW_ALT_F4=false`).

---

## 5. Audit Logging

All security-sensitive operations generate structured audit events stored in `audit_events` table:

```json
{
  "event_type": "auth.login.success",
  "user_id": 1,
  "ip_address": "127.0.0.1",
  "user_agent": "Mozilla/5.0 ...",
  "timestamp": "2026-09-14T10:00:00Z",
  "details": {"action": "login"}
}
```

> [!NOTE]
> Audit logs **never** record passwords, JWT tokens, refresh hashes, or clipboard contents.
