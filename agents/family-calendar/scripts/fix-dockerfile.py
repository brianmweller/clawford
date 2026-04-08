#!/usr/bin/env python3
"""Fix the VPS Dockerfile to add google-api packages."""

DOCKERFILE = "/home/openclaw/openclaw/Dockerfile"

with open(DOCKERFILE) as f:
    lines = f.readlines()

# First pass: remove any garbled google lines
clean_lines = []
for line in lines:
    if "google-api-python-client" in line:
        continue
    if "google-auth-httplib2" in line:
        continue
    if "google-auth-oauthlib" in line:
        continue
    clean_lines.append(line)

# Second pass: insert proper lines after curl_cffi
final_lines = []
for line in clean_lines:
    final_lines.append(line)
    if line.strip().startswith("curl_cffi"):
        final_lines.append("    google-api-python-client \\\n")
        final_lines.append("    google-auth-httplib2 \\\n")
        final_lines.append("    google-auth-oauthlib \\\n")

# Third pass: ensure && rm -rf exists after the pip block
# Check if it was lost
content = "".join(final_lines)
if "google-auth-oauthlib \\\n  && rm -rf" not in content:
    # The rm -rf line is missing, add it
    content = content.replace(
        "google-auth-oauthlib \\\n\n",
        "google-auth-oauthlib \\\n  && rm -rf /var/lib/apt/lists/*\n\n",
    )

with open(DOCKERFILE, "w") as f:
    f.write(content)

# Verify
with open(DOCKERFILE) as f:
    text = f.read()

ok = True
for pkg in ["google-api-python-client", "google-auth-httplib2", "google-auth-oauthlib"]:
    if pkg in text:
        print(f"  OK: {pkg} found")
    else:
        print(f"  MISSING: {pkg}")
        ok = False

if "rm -rf /var/lib/apt/lists" in text:
    print("  OK: rm -rf cleanup line present")
else:
    print("  MISSING: rm -rf cleanup line")
    ok = False

print("SUCCESS" if ok else "FAILED")
