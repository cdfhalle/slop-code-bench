#!/bin/bash
# Build an enroot "sysconf" mirror with two host-environment fixes, without root.
# enroot reads mounts.d/hooks.d/environ.d from $ENROOT_SYSCONF_PATH (default
# /etc/enroot); the main enroot.conf is read from a hardcoded /etc/enroot path
# regardless. So we mirror /etc/enroot -- mostly symlinks -- with two patched
# fstabs. Point ENROOT_SYSCONF_PATH at this mirror (slurm/enroot_env.sh); it is
# loaded by every container the harness starts.
#
# Fix 1 -- mute the spurious /scratch mount warning:
#   The site 30-slurm.fstab bind-mounts the host's /scratch into every container
#   when that line is present (`/scratch /scratch none x-create=dir,bind,...`).
#   enroot re-applies all fstabs on EVERY `enroot start`. cpu-batch nodes have no
#   /scratch unless the SCRATCH:NVME prolog created it, so the mount fails. It is
#   `nofail` (harmless) but not `silent`, so a warning is printed into every
#   command's output. We add `silent`. If the site file has no /scratch line the
#   sed is a no-op; we do not invent a mount that is not there.
#
# Fix 2 -- make `localhost` resolve to IPv4 (general, all languages):
#   enroot bind-mounts the host's /etc/hosts into the container (unlike Docker,
#   which gives each container its own). The host file maps `::1 localhost`, and
#   with no gai.conf glibc prefers IPv6, so `localhost` -> ::1. Servers in the
#   images bind 0.0.0.0 (IPv4 only), so any test that connects to a localhost
#   server gets `ECONNREFUSED ::1` and its whole suite fails. We bind a custom
#   /etc/hosts where `localhost` is IPv4-only.
#
# The mirror bakes an absolute path: mounts.d/20-config.fstab names this
# directory's etc/hosts as the bind source. Moving the checkout or the mirror
# without re-running this script leaves containers with a dangling hosts file.
# Idempotent: a second run deletes and rewrites the same tree.
#
# No Go cache bind. That was a swe-bench workaround; this image does not need it.
#
# Usage: setup_enroot_sysconf.sh [TARGET_DIR]
# Target defaults to $SCB_REPO/enroot_sysconf when config.sh has been sourced
# (or can be sourced next to this repo); otherwise the repo directory that
# contains this slurm/ folder.
set -euo pipefail

SRC=/etc/enroot
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DEFAULT=$(cd "$HERE/.." && pwd)

if [ -z "${SCB_REPO:-}" ] && [ -f "$REPO_DEFAULT/config.sh" ]; then
    # shellcheck source=../config.sh
    source "$REPO_DEFAULT/config.sh"
fi

TARGET="${1:-${SCB_REPO:-$REPO_DEFAULT}/enroot_sysconf}"

if [ ! -d "$SRC/mounts.d" ]; then
    echo "site enroot config not found at $SRC" >&2
    exit 1
fi

# Regenerating the mirror deletes TARGET. Refuse the few paths where that
# would destroy the site install or the filesystem root.
case "$TARGET" in
    "" | / | /etc | /etc/enroot | /etc/enroot/*)
        echo "refusing to rebuild enroot sysconf at: $TARGET" >&2
        exit 1
        ;;
esac

rm -rf "$TARGET"
mkdir -p "$TARGET/mounts.d" "$TARGET/etc"
TARGET=$(cd "$TARGET" && pwd) # enroot bind sources must be absolute

# Unchanged trees stay symlinks so a later site file is picked up. mounts.d is
# a real directory because two of its files are patched copies.
ln -s "$SRC/hooks.d" "$TARGET/hooks.d"
ln -s "$SRC/environ.d" "$TARGET/environ.d"

# Symlink every fstab we do not patch, so a site addition is not dropped.
for _f in "$SRC"/mounts.d/*; do
    [ -e "$_f" ] || continue
    _base=$(basename "$_f")
    case "$_base" in
        20-config.fstab | 30-slurm.fstab) continue ;;
    esac
    ln -s "$_f" "$TARGET/mounts.d/$_base"
done
unset _f _base

# Fix 2: IPv4-only-localhost /etc/hosts, bound in place of the host's via a
# patched 20-config.fstab (rewrite only the /etc/hosts mount SOURCE).
cat >"$TARGET/etc/hosts" <<'HOSTS'
127.0.0.1	localhost
::1		ip6-localhost ip6-loopback
ff02::1		ip6-allnodes
ff02::2		ip6-allrouters
HOSTS
sed -E "s#^/etc/hosts([[:space:]]+)/etc/hosts#${TARGET}/etc/hosts\\1/etc/hosts#" \
    "$SRC/mounts.d/20-config.fstab" >"$TARGET/mounts.d/20-config.fstab"

# Fix 1: add `silent` to the /scratch line of 30-slurm, copy the rest verbatim.
sed -E '\#^/scratch[[:space:]]#{ /silent/!s/(nofail)/\1,silent/ }' \
    "$SRC/mounts.d/30-slurm.fstab" >"$TARGET/mounts.d/30-slurm.fstab"

echo "Built enroot sysconf mirror at: $TARGET"
if grep -q '^/scratch' "$TARGET/mounts.d/30-slurm.fstab"; then
    echo "  /scratch line   : $(grep '^/scratch' "$TARGET/mounts.d/30-slurm.fstab")"
else
    echo "  /scratch line   : (none in site 30-slurm.fstab; nothing to silence)"
fi
echo "  /etc/hosts mount: $(grep '/etc/hosts' "$TARGET/mounts.d/20-config.fstab" | head -1)"
echo "  absolute hosts  : $TARGET/etc/hosts (baked into 20-config.fstab; rebuild after moving)"
