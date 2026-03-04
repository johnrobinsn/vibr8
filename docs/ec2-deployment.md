# EC2 Reverse Proxy Deployment

Access vibr8 from anywhere on the internet via an EC2 instance acting as a
TLS-terminating reverse proxy with SSH tunnels back to dev machines.

## Architecture

```
Browser (anywhere) --> EC2 nginx (TLS, port 443)
                        |
                        v
                      SSH tunnel (autossh)
                        |
                        v
                      Dev machine vibr8 (port 3456)
                        +-- REST API (auth cookie)
                        +-- WebSocket (auth cookie)
                        +-- WebRTC audio (via STUN/TURN on EC2)
```

## Prerequisites

- EC2 instance with a public IP and a domain (e.g. `vibr8.example.com`)
- SSH access from each dev machine to EC2
- DNS A record pointing the domain to the EC2 public IP

---

## 1. TLS with Let's Encrypt

```bash
# On EC2
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d vibr8.example.com
```

Certbot auto-renews via systemd timer.

---

## 2. nginx Configuration

`/etc/nginx/sites-available/vibr8`:

```nginx
upstream vibr8 {
    server 127.0.0.1:3456;
}

server {
    listen 443 ssl http2;
    server_name vibr8.example.com;

    ssl_certificate     /etc/letsencrypt/live/vibr8.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/vibr8.example.com/privkey.pem;

    # Proxy all traffic to the SSH tunnel
    location / {
        proxy_pass http://vibr8;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Long timeouts for WebSocket connections
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}

server {
    listen 80;
    server_name vibr8.example.com;
    return 301 https://$host$request_uri;
}
```

```bash
sudo ln -s /etc/nginx/sites-available/vibr8 /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## 3. SSH Tunnel (autossh)

Persistent reverse tunnel from each dev machine to EC2.

### Dev machine systemd service

`~/.config/systemd/user/vibr8-tunnel.service`:

```ini
[Unit]
Description=vibr8 reverse SSH tunnel to EC2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=AUTOSSH_GATETIME=0
ExecStart=/usr/bin/autossh -M 0 -N \
    -o "ServerAliveInterval 30" \
    -o "ServerAliveCountMax 3" \
    -o "ExitOnForwardFailure yes" \
    -R 127.0.0.1:3456:localhost:3456 \
    ec2-user@vibr8.example.com
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
sudo apt install autossh  # or brew install autossh on macOS
systemctl --user daemon-reload
systemctl --user enable --now vibr8-tunnel
systemctl --user status vibr8-tunnel
```

### Multiple dev machines

Use different remote ports per machine:

| Machine    | Remote port | Tunnel flag                              |
|------------|-------------|------------------------------------------|
| Main dev   | 3456        | `-R 127.0.0.1:3456:localhost:3456`       |
| MacBook    | 3457        | `-R 127.0.0.1:3457:localhost:3456`       |

Then configure nginx upstreams or location blocks per machine.

---

## 4. TURN Server (coturn)

Required for WebRTC audio when clients are behind symmetric NATs.

### Install on EC2

```bash
sudo apt install coturn
```

### Configuration

`/etc/turnserver.conf`:

```ini
# Network
listening-port=3478
tls-listening-port=5349
listening-ip=0.0.0.0
external-ip=<EC2_PUBLIC_IP>
relay-ip=<EC2_PUBLIC_IP>
min-port=49152
max-port=65535

# TLS (reuse Let's Encrypt certs)
cert=/etc/letsencrypt/live/vibr8.example.com/fullchain.pem
pkey=/etc/letsencrypt/live/vibr8.example.com/privkey.pem

# Authentication
use-auth-secret
static-auth-secret=<GENERATE_A_LONG_RANDOM_SECRET>

# Realm
realm=vibr8.example.com

# Logging
log-file=/var/log/turnserver.log
verbose
```

```bash
# Enable coturn service
sudo sed -i 's/#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/' /etc/default/coturn
sudo systemctl enable --now coturn
```

### EC2 Security Group

Open these ports:

| Port        | Protocol | Purpose        |
|-------------|----------|----------------|
| 443         | TCP      | HTTPS/WSS      |
| 3478        | TCP/UDP  | TURN           |
| 5349        | TCP      | TURN over TLS  |
| 49152-65535 | UDP      | TURN relay     |

---

## 5. ICE Server Configuration

On each dev machine, create `~/.companion/ice-servers.json`:

```json
[
  {
    "urls": ["stun:vibr8.example.com:3478"]
  },
  {
    "urls": ["turn:vibr8.example.com:3478"],
    "username": "vibr8",
    "credential": "<GENERATE_A_LONG_RANDOM_SECRET>"
  },
  {
    "urls": ["turns:vibr8.example.com:5349"],
    "username": "vibr8",
    "credential": "<GENERATE_A_LONG_RANDOM_SECRET>"
  }
]
```

Or set the `VIBR8_ICE_SERVERS` environment variable with the same JSON.

The server loads this at startup and serves it via `GET /api/webrtc/ice-servers`.
The browser fetches it before creating `RTCPeerConnection`.

### Time-limited TURN credentials

coturn's `use-auth-secret` uses HMAC-based temporary credentials. The username
is a Unix timestamp (expiry) and the credential is
`HMAC-SHA1(secret, timestamp)`. For a static setup, a fixed username/credential
pair with `lt-cred-mech` also works.

---

## 6. Authentication

vibr8 has built-in user authentication (opt-in). Set it up before exposing to
the internet:

```bash
# Add a user
uv run python -m server.manage_users add myuser

# List users
uv run python -m server.manage_users list

# Remove a user
uv run python -m server.manage_users remove myuser
```

Credentials are stored in `~/.companion/users.json` with bcrypt hashes.
When the file exists, all API/WebSocket endpoints require authentication via
session cookie. No file = no auth (local dev mode).

---

## Verification Checklist

- [ ] `curl -I https://vibr8.example.com` returns 200
- [ ] Login page appears when accessing from browser
- [ ] WebSocket connections work (chat messages flow)
- [ ] `GET /api/webrtc/ice-servers` returns STUN/TURN config
- [ ] WebRTC audio connects (check for `srflx` and `relay` ICE candidates)
- [ ] Terminal sessions work through the proxy
- [ ] SSH tunnel auto-reconnects after network interruption
