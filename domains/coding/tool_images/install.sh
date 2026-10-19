#!/bin/sh
# Only terminal recording tools and their required runtime dependencies.
set -eu
export DEBIAN_FRONTEND=noninteractive
mkdir -p /opt/rrsi-tools
if ! command -v apt-get >/dev/null; then
    echo 'rrsi tool images currently require Debian/Ubuntu apt; use original mode for other systems' >&2
    exit 1
fi
# Docker's cleanup hook would discard the shared archive cache after each install.
if [ -f /etc/apt/apt.conf.d/docker-clean ]; then
    mv /etc/apt/apt.conf.d/docker-clean /tmp/rrsi-docker-clean
    trap 'mv /tmp/rrsi-docker-clean /etc/apt/apt.conf.d/docker-clean' EXIT
fi
# Shared .deb filenames include package, exact version and architecture; apt also
# verifies hashes from the signed index. No dpkg state is ever cache-mounted.
apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
# Pin both tools in a build-cache lock, scoped to distro, release, architecture
# and source repositories. Reusing a lock never silently upgrades the tools.
if [ ! -s /rrsi-package-lock/tools ]; then
    for tool in tmux asciinema; do
        version=$(apt-cache policy "$tool" | awk '/Candidate:/ {print $2}')
        [ -n "$version" ] && [ "$version" != '(none)' ]
        printf '%s=%s\n' "$tool" "$version"
    done > /rrsi-package-lock/tools.tmp
    mv /rrsi-package-lock/tools.tmp /rrsi-package-lock/tools
fi
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' | sort > /opt/rrsi-tools/packages-before.tsv
# Intentionally split this controlled two-line package list into argv.
apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 \
    -o Binary::apt::APT::Keep-Downloaded-Packages=true \
    install -y --no-install-recommends $(cat /rrsi-package-lock/tools)
tmux -V > /opt/rrsi-tools/tmux-version.txt
asciinema --version > /opt/rrsi-tools/asciinema-version.txt
cp /rrsi-package-lock/tools /opt/rrsi-tools/packages.lock
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' | sort > /opt/rrsi-tools/packages-after.tsv
