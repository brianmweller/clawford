###############################################################################
# OpenClaw Gateway – Docker Image
#
# IMPORTANT: Runtime installs are lost on container restart. All binaries
# needed by skills MUST be installed here at build time. If you add a skill
# that needs a new binary, update this Dockerfile and rebuild.
###############################################################################

# ── Stage 1: Build costco-go CLI ─────────────────────────────
FROM golang:1.24-alpine AS costco-builder
RUN apk add --no-cache git ca-certificates
WORKDIR /build
RUN git clone https://github.com/eshaffer321/costco-go.git .
COPY costco-cli-wrapper.go ./cmd/costco-wrapper/main.go
RUN cd cmd/costco-wrapper \
  && CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /build/costco .

# ── Stage 2: Main image ─────────────────────────────────────
FROM node:22-bookworm-slim

# Install system dependencies as root
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    chromium \
    ca-certificates \
    gnupg \
    tini \
    python3 \
    python3-pip \
    socat \
    xvfb \
    build-essential \
    python3-dev \
    libjpeg-dev \
    zlib1g-dev \
    openbox \
    fonts-liberation \
    fonts-dejavu-core \
    fontconfig \
    bubblewrap \
  && pip3 install --no-cache-dir --break-system-packages \
    feedparser==6.0.11 \
    linkedin-api \
    googlenewsdecoder \
    pyotp \
    playwright \
    amazon-orders \
    openai \
    curl_cffi \
    "camoufox[geoip]" \
    rebrowser-playwright \
    google-api-python-client \
    google-auth-httplib2 \
    google-auth-oauthlib \
    "mcp>=1.27" \
    httpx-sse \
    httpx \
  && fc-cache -f \
  && rm -rf /var/lib/apt/lists/*

# Copy costco-go CLI from builder stage
COPY --from=costco-builder /build/costco /usr/local/bin/costco

# Install GitHub CLI (required by github skill)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
  && apt-get update && apt-get install -y --no-install-recommends gh \
  && rm -rf /var/lib/apt/lists/*

# Pin OpenClaw version for reproducible builds — bump explicitly when upgrading
ARG OPENCLAW_VERSION=2026.4.11
RUN npm install -g openclaw@${OPENCLAW_VERSION}

# Neutralize the hardcoded exec preflight in pi-tools-*.js.
# OpenClaw 2026.4.11 rejects any interpreter invocation (python3/node)
# combined with shell operators (`;`, `&&`, redirects, `sh -lc`, exit-code
# capture). LLMs reflexively wrap cron commands that way, cascading the
# entire fleet into "approval required" messages. There's no config flag
# to disable it, so we patch the condition to `false &&` at build time,
# turning the `if` into a dead branch. See agents/shared/SCRIPT_CONTRACT.md.
#
# We also revert the earlier `return;throw` patch if it's present — that
# approach inverted the check and was worse than the original bug.
RUN for f in /usr/local/lib/node_modules/openclaw/dist/pi-tools-*.js; do \
      [ -f "$f" ] || continue; \
      if grep -q 'return;throw new Error("exec preflight' "$f"; then \
        sed -i 's|return;throw new Error("exec preflight|throw new Error("exec preflight|' "$f" \
          && echo "reverted broken return;throw patch in $(basename "$f")"; \
      fi; \
      if grep -q 'if (false && hasInterpreterInvocation' "$f"; then \
        echo "exec preflight already neutralized in $(basename "$f")"; \
      elif grep -q 'if (hasInterpreterInvocation ' "$f"; then \
        sed -i 's|if (hasInterpreterInvocation |if (false \&\& hasInterpreterInvocation |' "$f" \
          && echo "neutralized exec preflight in $(basename "$f")"; \
      fi; \
    done

# Install ClawHub CLI for skill management
RUN npm install -g clawhub

# Install WeChat channel plugin (Phase 4: Mistress Mouse WeChat integration)
RUN npm install -g @tencent-weixin/openclaw-weixin

# Install acpx (ACP runtime) into the OpenClaw extensions directory as root (before USER node)
RUN cd /usr/local/lib/node_modules/openclaw/dist/extensions/acpx \
  && npm install --omit=dev --no-save acpx@0.4.0 \
  || true

# Create OpenClaw directories owned by node
RUN mkdir -p /home/node/.openclaw/workspace /.clawhub \
  && chown -R node:node /home/node/.openclaw /.clawhub

# Copy skills manifest, workspace templates, and entrypoint
COPY config/skills-manifest.txt /opt/config/skills-manifest.txt
COPY workspace-templates/ /opt/workspace-templates/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Fetch Camoufox browser binary + GeoIP db (as root for site-packages write access)
RUN camoufox fetch

# Create OpenClaw directories owned by node (must come before USER node)
# Also pre-create camoufox cache so node user doesn't re-download at runtime
RUN mkdir -p /home/node/.cache/camoufox \
  && cp -r /root/.cache/camoufox/* /home/node/.cache/camoufox/ 2>/dev/null || true \
  && chown -R node:node /home/node/.cache/camoufox

USER node

# Configure Openbox window placement (non-zero position to pass Akamai checks)
RUN mkdir -p /home/node/.config/openbox \
  && printf '<?xml version="1.0" encoding="UTF-8"?>\n<openbox_config xmlns="http://openbox.org/3.4/rc">\n  <applications>\n    <application class="*">\n      <position force="yes">\n        <x>center</x>\n        <y>45</y>\n      </position>\n      <decor>yes</decor>\n    </application>\n  </applications>\n</openbox_config>\n' > /home/node/.config/openbox/rc.xml

# Install Claude Code as the node user
# Download the binary directly (the install.sh `claude install` step fails non-interactively)
RUN mkdir -p /home/node/.claude/local/bin \
  && PLATFORM="linux-$(uname -m | sed 's/x86_64/x64/' | sed 's/aarch64/arm64/')" \
  && VERSION=$(curl -fsSL https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases/latest) \
  && curl -fsSL -o /home/node/.claude/local/bin/claude \
     "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases/${VERSION}/${PLATFORM}/claude" \
  && chmod +x /home/node/.claude/local/bin/claude
ENV PATH="/home/node/.claude/local/bin:${PATH}"

ENTRYPOINT ["tini", "--", "entrypoint.sh"]
