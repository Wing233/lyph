#!/bin/sh
# Host-only renewal entry point; the Certbot container never invokes host Docker.
set -eu
umask 077

dry_run=0
case "$#:$*" in
    0:) ;;
    1:--dry-run) dry_run=1 ;;
    1:--help|1:-h)
        printf '%s\n' 'Usage: renew-certificate.sh [--dry-run]'
        exit 0
        ;;
    *)
        printf '%s\n' 'Usage: renew-certificate.sh [--dry-run]' >&2
        exit 2
        ;;
esac

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
certificate_name=${LYPH_CERT_NAME:-}
if [ -z "$certificate_name" ]; then
    printf '%s\n' 'LYPH_CERT_NAME is required (domain or IP).' >&2
    exit 2
fi
case "$certificate_name" in
    ''|.|..|*[!A-Za-z0-9._-]*)
        printf '%s\n' 'Invalid LYPH_CERT_NAME.' >&2
        exit 2
        ;;
esac
certificate_dir="/etc/letsencrypt/live/$certificate_name"
reload_marker=/var/lib/letsencrypt/reload-nginx
certbot_image=certbot/certbot:v5.8.0@sha256:f70ad0adbb7e117f0fe42a63c553f28ea451edabc0148757b6efcd9735acaa20

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Certificate renewal must run as root on the host.' >&2
    exit 1
fi
for program in docker flock; do
    if ! command -v "$program" >/dev/null 2>&1; then
        printf 'Required host program is unavailable: %s\n' "$program" >&2
        exit 1
    fi
done
for required_file in "$project_dir/deploy/install-certificate.sh" \
    "/etc/letsencrypt/renewal/$certificate_name.conf" \
    "$certificate_dir/fullchain.pem" "$certificate_dir/privkey.pem"; do
    if [ ! -r "$required_file" ]; then
        printf 'Required renewal file is unavailable: %s\n' "$required_file" >&2
        exit 1
    fi
done

mkdir -p /var/lib/letsencrypt /var/log/letsencrypt "$project_dir/runtime/acme"
# Challenges are public and must be traversable by the unprivileged Nginx container.
chmod 755 "$project_dir/runtime/acme"
exec 9>/var/lib/letsencrypt/lyph-renew.lock
if ! flock -n 9; then
    printf '%s\n' 'Another certificate renewal is running; skipping this invocation.'
    exit 0
fi
if [ -L "$reload_marker" ]; then
    printf '%s\n' 'Renewal marker must not be a symbolic link.' >&2
    exit 1
fi

# Select one lineage so the host can recover its exact path without trusting file content.
# Directory hooks are excluded; the only container deploy hook touches this shared marker.
set -- renew --non-interactive --cert-name "$certificate_name" \
    --webroot --webroot-path /var/www/acme --required-profile shortlived \
    --no-directory-hooks --deploy-hook 'touch /var/lib/letsencrypt/reload-nginx'
if [ "$dry_run" -eq 1 ]; then
    # Deploy hooks are disabled by Certbot during dry-run. Do not consume any pending marker.
    # Explicit staging prevents a saved/global production server override from changing this test.
    set -- "$@" --dry-run --server https://acme-staging-v02.api.letsencrypt.org/directory
fi

if docker run --rm --name lyph-certificate-renew --platform linux/amd64 \
    --memory 192m --memory-swap 192m --cpus 0.5 --pids-limit 64 \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --mount type=bind,src=/etc/letsencrypt,dst=/etc/letsencrypt \
    --mount type=bind,src=/var/lib/letsencrypt,dst=/var/lib/letsencrypt \
    --mount type=bind,src=/var/log/letsencrypt,dst=/var/log/letsencrypt \
    --mount "type=bind,src=$project_dir/runtime/acme,dst=/var/www/acme" \
    "$certbot_image" "$@"; then
    :
else
    status=$?
    printf '%s\n' 'Certbot renewal failed; a pending reload marker has been retained.' >&2
    exit "$status"
fi

if [ "$dry_run" -eq 1 ]; then
    printf '%s\n' 'Certbot staging renewal rehearsal succeeded; production certificates were not reloaded.'
    exit 0
fi
if [ -f "$reload_marker" ]; then
    if RENEWED_LINEAGE="$certificate_dir" /bin/sh "$project_dir/deploy/install-certificate.sh"; then
        rm -- "$reload_marker"
        printf '%s\n' 'Renewed certificate installed and Nginx reloaded.'
    else
        status=$?
        printf '%s\n' 'Certificate installation/reload failed; the marker is retained for retry.' >&2
        exit "$status"
    fi
else
    printf '%s\n' 'No renewed certificate is awaiting installation.'
fi
