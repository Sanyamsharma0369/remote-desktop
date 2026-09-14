# Online Deployment Guide

## The Right First Deployment: VPN-Only Access

The **safest and simplest** first deployment is through a private VPN overlay. Your Remote Desktop app is never publicly discoverable — only devices on your VPN can reach it.

### Option A — Tailscale (Recommended for beginners)

```powershell
# 1. Install Tailscale on the Windows host running this app
winget install Tailscale.Tailscale

# 2. Sign in and connect the host machine
tailscale up

# 3. Get your Tailscale IP (e.g. 100.x.x.x)
tailscale ip

# 4. Install Tailscale on any controller devices (phone, laptop, etc.)
# 5. Update .env to bind to VPN interface
# HOST=100.x.x.x   (your Tailscale IP)
# Or keep HOST=0.0.0.0 and restrict with Windows Firewall

# 6. Start the app
python server.py

# 7. Access from any VPN device at:
# http://100.x.x.x:9005
```

Tailscale provides encrypted WireGuard tunnels. You still get HTTPS if you use Tailscale's HTTPS feature (`tailscale cert`).

### Option B — WireGuard

Similar approach: install WireGuard on host + controller, assign private subnet IPs, bind app to WireGuard interface IP.

---

## Public Deployment Prerequisites

Before exposing this to the public internet, every item below must be complete:

### 1. Domain and Valid TLS

- Register a domain (e.g. `rd.yourdomain.com`)
- Point DNS A record to your server IP
- Use **Caddy** (recommended) or Nginx + Certbot for automatic Let's Encrypt certificates
- All traffic must be HTTPS/WSS — never plain HTTP/WS in production

### 2. Set Production `.env`

```bash
APP_ENV=production
HOST=127.0.0.1           # Bind only to loopback — Caddy is the public entry
PORT=9005
PUBLIC_ORIGIN=https://rd.yourdomain.com
ALLOWED_ORIGINS=https://rd.yourdomain.com
ALLOWED_WS_ORIGINS=https://rd.yourdomain.com
COOKIE_SECURE=true
COOKIE_SAMESITE=strict
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7
SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
```

### 3. Windows Firewall

```powershell
# Allow only Caddy's ports publicly — block direct access to 9005
New-NetFirewallRule -DisplayName "Block 9005 inbound" -Direction Inbound -LocalPort 9005 -Protocol TCP -Action Block
New-NetFirewallRule -DisplayName "Allow HTTPS 443" -Direction Inbound -LocalPort 443 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Allow HTTP 80" -Direction Inbound -LocalPort 80 -Protocol TCP -Action Allow
```

### 4. Caddy Reverse Proxy (Linux deployment)

```bash
# Install Caddy
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# Use the Caddyfile in deploy/Caddyfile
export DOMAIN=rd.yourdomain.com
sudo caddy run --config deploy/Caddyfile
```

### 5. CORS and WebSocket Origin Updates

When moving from localhost to production, update `.env`:

```bash
# Development (current)
ALLOWED_ORIGINS=http://localhost:9005,http://127.0.0.1:9005
ALLOWED_WS_ORIGINS=http://localhost:9005,http://127.0.0.1:9005

# Production (HTTPS)
ALLOWED_ORIGINS=https://rd.yourdomain.com
ALLOWED_WS_ORIGINS=https://rd.yourdomain.com
```

The `get_settings()` call is LRU-cached — restart the server after `.env` changes.

### 6. Strong Authentication

- Change the default admin password immediately after first start
- Enable MFA/passkeys before public exposure (not yet implemented — see roadmap)
- Use unique, strong passwords

### 7. TURN Server (only if needed for cross-network WebRTC)

WebRTC connects peer-to-peer when possible. You only need TURN if:
- Viewer and host are on different NAT networks
- Direct UDP is blocked

```bash
# If TURN is needed, use the example config:
cp deploy/turnserver.conf.example /etc/coturn/turnserver.conf
# Edit the file: set your IP, domain, and generate a STRONG static-auth-secret
openssl rand -base64 32   # use this as static-auth-secret

# Start coturn
systemctl enable coturn
systemctl start coturn
```

**Never run an unauthenticated TURN server.**

### 8. Backups

```bash
# SQLite database backup (daily cron example)
cp rd_app.db rd_app.db.backup.$(date +%Y%m%d)

# Or use a proper backup tool:
sqlite3 rd_app.db ".backup /backups/rd_app_$(date +%Y%m%d).db"
```

### 9. Dependency Updates

```bash
pip list --outdated
pip install --upgrade -r requirements.txt
```

Run this regularly and test after upgrades.

---

## Rollback Plan

1. Stop the server: `Ctrl+C` or `systemctl stop rd-app`
2. Restore backup DB: `cp rd_app.db.backup.YYYYMMDD rd_app.db`
3. Restore previous code: `git checkout <previous-commit>`
4. Restart: `python server.py`

---

## What Remains Unsafe Without Additional Work

| Risk | Mitigation Needed |
|---|---|
| Single-factor auth | Implement MFA / passkeys |
| No host consent dialog | Implement host-side approval for control |
| No device pairing | Implement device pairing codes |
| No audit log UI | Build admin audit log viewer |
| No automatic control expiry | Implement server-side control session timeout |
| No session recording | Optional — implement if audit trail required |
| SQLite not suitable for high concurrency | Migrate to PostgreSQL for multi-user scale |

---

## Windows-Specific Notes

- Use `python server.py` (or `python -m uvicorn app.main:app ...`) from the backend directory
- The app captures the screen using `mss` and injects input via `pyautogui` — it must run on the physical Windows session, not a remote desktop session of its own
- Firewall rules use `New-NetFirewallRule` (PowerShell) as shown above
- For service installation: use NSSM (`nssm install RDApp python server.py`) or Windows Task Scheduler

---

## Production Readiness Checklist

- [ ] APP_ENV=production in .env
- [ ] HOST=127.0.0.1 (loopback only)
- [ ] COOKIE_SECURE=true
- [ ] Strong SECRET_KEY (≥32 bytes, random)
- [ ] HTTPS domain configured
- [ ] Caddy or Nginx running on :443
- [ ] HTTP :80 redirects to HTTPS
- [ ] Windows Firewall blocks port 9005 from public
- [ ] Default admin password changed
- [ ] Audit log reviewed after first sessions
- [ ] Dependency scan completed
- [ ] Backups configured
