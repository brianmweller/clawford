# Chapter 1: VPS Setup

Terraform + Docker. If you set this up manually, you'll forget what you did and can't reproduce it.

---

## The Terraform Approach (Recommended)

The [openclaw-terraform-hetzner](https://github.com/andreesg/openclaw-terraform-hetzner) repository automates server provisioning, Docker installation, firewall configuration, automated backups, and optional Tailscale VPN. It also uses a companion [openclaw-docker-config](https://github.com/andreesg/openclaw-docker-config) repository for the Docker image and OpenClaw configuration.

### Prerequisites

Install on your local machine:

```bash
# Terraform
winget install HashiCorp.Terraform   # Windows
brew install terraform                # macOS

# Hetzner CLI
winget install hetznercloud.cli      # Windows
brew install hcloud                   # macOS
```

You'll also need:
- A Hetzner Cloud API token (generate at console.hetzner.cloud → Security → API Tokens)
- A GitHub Personal Access Token with `read:packages` scope (for pulling Docker images)
- An SSH key pair (`ssh-keygen -t ed25519` if you don't have one)

### Step 1: Clone the repos

```bash
git clone https://github.com/andreesg/openclaw-terraform-hetzner.git infra
git clone https://github.com/andreesg/openclaw-docker-config.git openclaw-docker-config
```

### Step 2: Upload your SSH key to Hetzner

```bash
hcloud context create openclaw   # Paste your API token when prompted
hcloud ssh-key create --name mykey --public-key-from-file ~/.ssh/id_ed25519.pub
hcloud ssh-key list              # Note the fingerprint
```

### Step 3: Configure inputs

```bash
cp infra/config/inputs.example.sh infra/config/inputs.sh
```

Edit `infra/config/inputs.sh`:

```bash
export HCLOUD_TOKEN="your-hetzner-api-token"
export TF_VAR_hcloud_token="$HCLOUD_TOKEN"
export TF_VAR_ssh_key_fingerprint="your-ssh-key-fingerprint"
export TF_VAR_ssh_allowed_cidrs='["0.0.0.0/0"]'
export CONFIG_DIR="/path/to/openclaw-docker-config"
export GHCR_USERNAME="your-github-username"
export GHCR_TOKEN="your-github-pat"
```

### Step 4: Configure secrets

```bash
cp infra/secrets/openclaw.env.example infra/secrets/openclaw.env
```

Edit `infra/secrets/openclaw.env`:

```bash
OPENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)  # Generate and paste the result
OPENCLAW_GATEWAY_PORT=18789
OPENCLAW_GATEWAY_BIND=0.0.0.0
TELEGRAM_BOT_TOKEN=         # Fill in after Chapter 4
```

### Step 5: Provision the VPS

```bash
cd infra/infra/terraform/envs/prod

# On Windows PowerShell, set env vars manually:
$env:HCLOUD_TOKEN = "your-token"
$env:TF_VAR_hcloud_token = $env:HCLOUD_TOKEN
$env:TF_VAR_ssh_key_fingerprint = "your-fingerprint"
$env:TF_VAR_ssh_allowed_cidrs = '["0.0.0.0/0"]'
$env:TF_VAR_enable_tailscale = "false"
$env:TF_VAR_tailscale_auth_key = ""

# On Linux/macOS:
source ../../config/inputs.sh

terraform init
terraform plan    # Review — should create VPS + firewall
terraform apply   # Type 'yes'
```

Note the `server_ip` from the output.

### Step 6: Verify SSH access

```bash
ssh -i ~/.ssh/id_ed25519 root@{server_ip} "echo CONNECTED"
```

> **WARNING:** If this asks for a password, your SSH key wasn't attached during provisioning. Check that `TF_VAR_ssh_key_fingerprint` matches your key and re-run `terraform apply`. You may need to `terraform destroy` and re-create.

### Step 7: Set up Docker and OpenClaw

SSH into the VPS and verify Docker is installed (cloud-init should have handled this):

```bash
ssh -i ~/.ssh/id_ed25519 root@{server_ip}
docker --version
id openclaw        # Should exist with UID 1000
usermod -aG docker openclaw
exit
```

Transfer the Docker config and start the container:

```bash
# From your local machine
scp -i ~/.ssh/id_ed25519 openclaw-docker-config/docker/docker-compose.yml openclaw@{server_ip}:~/openclaw/
scp -i ~/.ssh/id_ed25519 openclaw-docker-config/docker/Dockerfile openclaw@{server_ip}:~/openclaw/
scp -i ~/.ssh/id_ed25519 openclaw-docker-config/docker/entrypoint.sh openclaw@{server_ip}:~/openclaw/
scp -i ~/.ssh/id_ed25519 -r openclaw-docker-config/config openclaw@{server_ip}:~/openclaw/
scp -i ~/.ssh/id_ed25519 -r openclaw-docker-config/workspace-templates openclaw@{server_ip}:~/openclaw/
scp -i ~/.ssh/id_ed25519 infra/secrets/openclaw.env openclaw@{server_ip}:~/openclaw/.env

# SSH in and build
ssh -i ~/.ssh/id_ed25519 openclaw@{server_ip}
cd ~/openclaw
chmod +x docker/entrypoint.sh entrypoint.sh 2>/dev/null
docker compose build --no-cache
docker compose up -d
sleep 60  # Wait for skill installation
docker compose logs --tail 5
```

You should see `listening on ws://0.0.0.0:18789`.

> **WARNING:** If the entrypoint fails with `bash\r: No such file or directory`, the script has Windows line endings. Fix with: `sed -i 's/\r$//' ~/openclaw/docker/entrypoint.sh ~/openclaw/entrypoint.sh` and rebuild.

### Step 8: The `oc()` helper function

All OpenClaw CLI commands run inside the Docker container. Add this to your shell:

```bash
oc() { docker compose -f ~/openclaw/docker-compose.yml exec -T openclaw-gateway openclaw "$@"; }
oci() { docker compose -f ~/openclaw/docker-compose.yml exec -it openclaw-gateway openclaw "$@"; }
```

Add these to `~/.bashrc` so they persist across sessions.

### Step 9: Verify

```bash
oc health
```

Should show: `Telegram: ok`, `Agents: main (default)`.

### Step 10: Install Claude Code (optional but recommended)

Claude Code on the VPS gives you a powerful debugging escape hatch for when OpenClaw itself breaks:

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

### Step 11: Install Tailscale (recommended)

Tailscale creates an encrypted mesh VPN between your VPS, local machine, and phone. It provides a safer alternative to public SSH.

```bash
# On the VPS
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up
```

If it prints an auth URL, open it in your browser and log in. If your Tailscale account is already linked, it auto-authenticates.

Verify:

```bash
sudo tailscale status
```

You should see your VPS (`<your-tailscale-host>`) and your local machine (`thinkpadbri`) on the same tailnet.

### Step 12: Access from your local machine

Open an SSH tunnel to access the gateway UI:

```bash
ssh -N -L 18789:127.0.0.1:18789 openclaw@{server_ip}
```

Then browse to `http://localhost:18789/` and paste your gateway token.

---

Next: [Chapter 2 — Shared Brain](02-shared-brain.md)
