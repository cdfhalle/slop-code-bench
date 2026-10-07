#!/bin/bash
# Bake the slop-code base sqsh, and optionally one agent layer, on cpu-batch.
#
#   mkdir -p logs    # Slurm opens --output before the script runs
#   sbatch slurm/bake_image.sbatch
#   SCB_BAKE_AGENT=claude_code SCB_AGENT_VERSION=2.0.51 sbatch slurm/bake_image.sbatch
#
# Those two variables must be exported (sbatch --export=ALL only forwards the
# environment). The base image is public GHCR. This script does not pull Docker
# Hub and does not implement the dh1/dh2 failover.
#
# Account, partition, and constraint below match config.sh. Slurm reads them
# before the script runs, so an override has to be an sbatch flag, not an export.
#SBATCH --job-name=scb-bake
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=pot-hpi-cpu-batch
#SBATCH --constraint=ARCH:X86&SCRATCH:NVME
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=04:00:00
# First bake: job 2611782, elapsed 00:02:13, MaxRSS 8619592K (~8.2G) on cx42.
# Request stays above that: a cold apt/node/rust download can exceed it.
#SBATCH --output=logs/%x-%j.out

set -euo pipefail

# Print one shell command for an agent docker.j2, or exit 2 naming the file.
# Accepts a single substantive RUN that is npm install, pip install, or
# `curl … | sh`/`bash`. chmod/mkdir/ln/chown RUNs do not count. Anything else
# (several install RUNs, uv tool, apt plus a venv) is refused rather than guessed.
extract_agent_install() {
    local j2=$1 version=$2
    python3 - "$j2" "$version" <<'PY'
import re
import sys

path, version = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
joined: list[str] = []
buf = ""
for line in text.splitlines():
    if buf:
        buf += " " + line.strip()
    else:
        buf = line.rstrip()
    if buf.endswith("\\"):
        buf = buf[:-1].rstrip()
        continue
    joined.append(buf)
    buf = ""
if buf:
    joined.append(buf)

runs: list[str] = []
for line in joined:
    match = re.match(r"\s*RUN\s+(.*)$", line)
    if match:
        runs.append(match.group(1).strip())

def substantive(cmd: str) -> bool:
    parts = [part.strip() for part in re.split(r"\s*&&\s*", cmd)]
    trivial = re.compile(r"(chmod|mkdir|ln|chown|true)\b")
    return any(part and not trivial.match(part) for part in parts)

kept = [cmd for cmd in runs if substantive(cmd)]
simple = re.compile(
    r"(npm install\b|pip install\b|curl\b[^|&]*\|\s*(sh|bash)\b)"
)
if len(kept) == 1 and simple.search(kept[0]):
    cmd = re.sub(r"\{\{\s*version\s*\}\}", version, kept[0])
    if re.search(r"\{\{|\{%", cmd):
        print(f"unresolved template in {path}", file=sys.stderr)
        sys.exit(2)
    print(cmd)
    sys.exit(0)
print(
    f"agent docker.j2 is not a single npm/pip/curl install: {path}",
    file=sys.stderr,
)
sys.exit(2)
PY
}

# enroot --mount splits on ':'. TMPDIR on this cluster does not contain one;
# bail out rather than produce a garbled fstab line.
assert_mountable() {
    case "$1" in
        *:*)
            echo "path contains a colon, cannot pass it to enroot --mount: $1" >&2
            exit 1
            ;;
    esac
}

refuse_same_store() {
    local store images
    store=$(realpath -m "${SCB_IMAGE_STORE%/}")
    images=$(realpath -m "${SCB_IMAGES_DIR%/}")
    if [ -z "$store" ] || [ -z "$images" ] || [ "$store" = "$images" ]; then
        echo "refusing to export: SCB_IMAGE_STORE must not equal SCB_IMAGES_DIR ($store)" >&2
        exit 1
    fi
}

main() {
    cd "${SCB_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
    # shellcheck source=../config.sh
    source ./config.sh
    # shellcheck source=enroot_env.sh
    source ./slurm/enroot_env.sh

    if [ -z "${SCB_IMAGE_STORE}" ] || [ -z "${SCB_IMAGES_DIR}" ]; then
        echo "SCB_IMAGE_STORE and SCB_IMAGES_DIR must be set" >&2
        exit 1
    fi
    # Fail before the pull. Export checks again in case the paths were retargeted.
    refuse_same_store

    bash slurm/setup_enroot_sysconf.sh
    mkdir -p "$SCB_IMAGES_DIR"

    local base_sqsh="$SCB_IMAGES_DIR/uv-python3.12-trixie-slim.sqsh"
    if [ ! -f "$base_sqsh" ]; then
        # Public GHCR, not Docker Hub. A Docker Hub pull must use
        # docker://registry-1.docker.io#... and authenticated credentials from
        # ~/.config/slop-code/.env, because the cluster NAT IP's anonymous
        # quota is spent. Do not implement the dh1/dh2 failover in this first
        # bake; the base image is GHCR.
        if ! enroot import -o "$base_sqsh" \
            docker://ghcr.io/astral-sh/uv:python3.12-trixie-slim; then
            rm -f "$base_sqsh"
            exit 1
        fi
    fi

    # Fixed name. ENROOT_DATA_PATH is per-job when SLURM_SCRATCH is, so a
    # second bake on a different node does not collide. Do not run two bakes
    # that share one scratch.
    # Global, not local: the EXIT trap runs after main returns, and a local
    # would already be gone, so the rootfs would leak.
    BAKE_CONTAINER=scb-bake-base
    # shellcheck disable=SC2317,SC2329
    cleanup() {
        local status=$?
        if [ -n "${BAKE_CONTAINER:-}" ]; then
            enroot remove -f "$BAKE_CONTAINER" >/dev/null 2>&1 || true
        fi
        exit "$status"
    }
    trap cleanup EXIT

    local attempt
    for attempt in 1 2 3; do
        if enroot create --name "$BAKE_CONTAINER" "$base_sqsh"; then
            break
        fi
        echo "enroot create failed (attempt ${attempt}/3)" >&2
        # A partial rootfs from the failed attempt blocks the next create.
        enroot remove -f "$BAKE_CONTAINER" >/dev/null 2>&1 || true
        if [ "$attempt" -eq 3 ]; then
            exit 1
        fi
        sleep $((attempt * 5))
    done

    local base_script="$TMPDIR/slop-bake-base.sh"
    assert_mountable "$base_script"
    cat >"$base_script" <<'END_BASE'
set -euo pipefail
# Translated from src/slop_code/execution/docker_runtime/setup_base.docker.j2.
# Differences, on purpose:
#   HOME, CARGO_HOME, and RUSTUP_HOME live inside the image at /opt/agent_home
#   (mode a+rwX). Not a host path, and not /tmp: enroot hides /tmp with a tmpfs
#   on every start, so a rustup installed there would vanish.
#   UV_CACHE_DIR, PIP_CACHE_DIR, and PIP_CONFIG_FILE are /opt/cache/... for the
#   same reason. The j2 only names the pip config path; the file is created
#   here so pip has a cache-dir inside the image.
# enroot does not keep the environment of this start the way Docker ENV does.
# Imported image ENV is /etc/environment, so the values are written there and
# into /etc/profile.d for login shells.

export DEBIAN_FRONTEND=noninteractive
export HOME=/opt/agent_home
export CARGO_HOME=/opt/agent_home/.cargo
export RUSTUP_HOME=/opt/agent_home/.rustup
export NVM_DIR=/usr/local/nvm
export NODE_VERSION=22.21.1
export UV_CACHE_DIR=/opt/cache/uv
export PIP_CACHE_DIR=/opt/cache/pip
export PIP_CONFIG_FILE=/opt/cache/pip.conf

apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends build-essential \
    && apt-get install -y --no-install-recommends \
        bzip2 \
        tar \
        gzip \
        zip \
        unzip \
        curl \
        ca-certificates \
        gnupg \
        default-jre-headless \
        netcat-openbsd \
        protobuf-compiler \
        procps \
        git \
        graphviz \
        rabbitmq-server \
        redis-server \
        docker-cli \
    && apt-get install -y --no-install-recommends \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
        libxss1 \
        libxtst6 \
        fonts-liberation \
        xdg-utils \
    && (apt-get install -y --no-install-recommends docker-compose-plugin \
        || apt-get install -y --no-install-recommends docker-compose-v2 \
        || apt-get install -y --no-install-recommends docker-compose) \
    && mkdir -p /usr/local/lib/docker/cli-plugins /usr/lib/docker/cli-plugins \
    && if [ -x /usr/bin/docker-compose ]; then \
        ln -sf /usr/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose; \
        ln -sf /usr/bin/docker-compose /usr/lib/docker/cli-plugins/docker-compose; \
    fi \
    && rm -rf /var/lib/apt/lists/*

curl -sSL https://dl.min.io/server/minio/release/linux-amd64/minio \
    -o /usr/local/bin/minio
chmod +x /usr/local/bin/minio

mkdir -p "$NVM_DIR"
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
# shellcheck disable=SC1091
. "$NVM_DIR/nvm.sh"
nvm install "$NODE_VERSION"
nvm alias default "$NODE_VERSION"
nvm use "$NODE_VERSION"
node_path=$(nvm which node)
node_bin=$(dirname "$node_path")
ln -s "$node_bin/node" /usr/local/bin/node
ln -s "$node_bin/npm" /usr/local/bin/npm
ln -s "$node_bin/npx" /usr/local/bin/npx

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

groupadd -g 1000 agent
useradd -m -u 1000 -g 1000 -d "$HOME" -s /bin/bash agent
getent group docker >/dev/null || groupadd docker
usermod -aG docker agent
mkdir -p /workspace /opt/cache/uv /opt/cache/pip
cat >/opt/cache/pip.conf <<'PIP'
[global]
cache-dir = /opt/cache/pip
PIP
chown -R agent:agent "$HOME" /workspace
chmod -R a+rwX "$HOME" /workspace /opt/cache

persist() {
    local key=$1 value=$2 file=/etc/environment
    touch "$file"
    grep -v "^${key}=" "$file" >"$file.tmp" || true
    mv "$file.tmp" "$file"
    printf '%s=%s\n' "$key" "$value" >>"$file"
}
persist HOME "$HOME"
persist CARGO_HOME "$CARGO_HOME"
persist RUSTUP_HOME "$RUSTUP_HOME"
persist NVM_DIR "$NVM_DIR"
persist NODE_VERSION "$NODE_VERSION"
persist UV_CACHE_DIR "$UV_CACHE_DIR"
persist PIP_CACHE_DIR "$PIP_CACHE_DIR"
persist PIP_CONFIG_FILE "$PIP_CONFIG_FILE"
if grep -q '^PATH=' /etc/environment; then
    current=$(grep '^PATH=' /etc/environment | head -1 | cut -d= -f2- | tr -d '"')
else
    current=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
fi
case ":$current:" in
    *":/opt/agent_home/.cargo/bin:"*) cargo_path=$current ;;
    *) cargo_path="/opt/agent_home/.cargo/bin:$current" ;;
esac
persist PATH "$cargo_path"
cat >/etc/profile.d/slop-code-bake.sh <<EOF
export HOME=$HOME
export CARGO_HOME=$CARGO_HOME
export RUSTUP_HOME=$RUSTUP_HOME
export NVM_DIR=$NVM_DIR
export UV_CACHE_DIR=$UV_CACHE_DIR
export PIP_CACHE_DIR=$PIP_CACHE_DIR
export PIP_CONFIG_FILE=$PIP_CONFIG_FILE
export PATH="$cargo_path"
EOF
chmod 644 /etc/profile.d/slop-code-bake.sh
END_BASE

    # --root: apt-get and writes under /usr need uid 0. enroot otherwise runs
    # as the host uid. Do not pass --net: that is a new netns with loopback
    # only, and the bake needs the host route for apt, curl, nvm, and rustup.
    # The script is mounted at /opt, not /tmp, because enroot covers /tmp with
    # a tmpfs. bash reads the file, so a noexec host mount still works.
    # x-create=file: the mount point is not in the base image. enroot --mount
    # splits on ':', so the option string must not contain one.
    enroot start --root --rw \
        -m "${base_script}:/opt/slop-bake-base.sh:none:x-create=file,bind,ro" \
        "$BAKE_CONTAINER" /bin/bash /opt/slop-bake-base.sh

    mkdir -p "$SCB_IMAGE_STORE"
    refuse_same_store
    # -f: a re-bake replaces the store object instead of failing on it.
    enroot export -f -o "$SCB_IMAGE_STORE/slop-code-python3.12.sqsh" "$BAKE_CONTAINER"
    echo "exported $SCB_IMAGE_STORE/slop-code-python3.12.sqsh"

    if [ -n "${SCB_BAKE_AGENT:-}" ] || [ -n "${SCB_AGENT_VERSION:-}" ]; then
        if [ -z "${SCB_BAKE_AGENT:-}" ] || [ -z "${SCB_AGENT_VERSION:-}" ]; then
            echo "set both SCB_BAKE_AGENT and SCB_AGENT_VERSION, or neither" >&2
            exit 2
        fi
        case "$SCB_BAKE_AGENT" in
            *[!A-Za-z0-9._-]*)
                echo "SCB_BAKE_AGENT must be a single path component" >&2
                exit 2
                ;;
        esac
        case "$SCB_AGENT_VERSION" in
            *[!A-Za-z0-9._+-]*)
                echo "SCB_AGENT_VERSION must be a single path component" >&2
                exit 2
                ;;
        esac
        local j2="src/slop_code/agent_runner/agents/${SCB_BAKE_AGENT}/docker.j2"
        if [ ! -f "$j2" ]; then
            echo "no agent docker.j2: $j2" >&2
            exit 2
        fi
        local install_cmd
        if ! install_cmd=$(extract_agent_install "$j2" "$SCB_AGENT_VERSION"); then
            exit 2
        fi
        # claude_code's docker.j2 is `npm install -g @anthropic-ai/claude-code@<version>`
        # (plus the router package on that same RUN). HOME and NPM_CONFIG_PREFIX
        # are set inside the image, not from the host.
        local agent_script="$TMPDIR/slop-bake-agent.sh"
        assert_mountable "$agent_script"
        {
            cat <<'END_HEAD'
set -euo pipefail
export HOME=/opt/agent_home
export NPM_CONFIG_PREFIX="$HOME/.npm-global"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"
export PATH="$NPM_CONFIG_PREFIX/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
mkdir -p "$NPM_CONFIG_PREFIX" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$HOME/.local/bin"
END_HEAD
            printf '%s\n' "$install_cmd"
            cat <<'END_TAIL'
chmod -R a+rwX "$HOME" "$NPM_CONFIG_PREFIX"
persist() {
    local key=$1 value=$2 file=/etc/environment
    touch "$file"
    grep -v "^${key}=" "$file" >"$file.tmp" || true
    mv "$file.tmp" "$file"
    printf '%s=%s\n' "$key" "$value" >>"$file"
}
persist HOME "$HOME"
persist NPM_CONFIG_PREFIX "$NPM_CONFIG_PREFIX"
persist XDG_CONFIG_HOME "$XDG_CONFIG_HOME"
persist XDG_DATA_HOME "$XDG_DATA_HOME"
if grep -q '^PATH=' /etc/environment; then
    current=$(grep '^PATH=' /etc/environment | head -1 | cut -d= -f2- | tr -d '"')
else
    current=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
fi
case ":$current:" in
    *":$NPM_CONFIG_PREFIX/bin:"*) new_path=$current ;;
    *) new_path="$NPM_CONFIG_PREFIX/bin:$HOME/.local/bin:$current" ;;
esac
persist PATH "$new_path"
END_TAIL
        } >"$agent_script"

        enroot start --root --rw \
            -m "${agent_script}:/opt/slop-bake-agent.sh:none:x-create=file,bind,ro" \
            "$BAKE_CONTAINER" /bin/bash /opt/slop-bake-agent.sh
        local agent_sqsh="$SCB_IMAGE_STORE/slop-code-${SCB_BAKE_AGENT}-${SCB_AGENT_VERSION}.sqsh"
        refuse_same_store
        enroot export -f -o "$agent_sqsh" "$BAKE_CONTAINER"
        echo "exported $agent_sqsh"
    fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main
fi
