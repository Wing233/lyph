#!/bin/sh
# Certbot deploy hook; source directory comes from Certbot, not browser input.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
certificate_dir=${RENEWED_LINEAGE:?Certbot RENEWED_LINEAGE is required}
test -r "$certificate_dir/fullchain.pem"
test -r "$certificate_dir/privkey.pem"
mkdir -p "$project_dir/runtime/tls"
chmod 700 "$project_dir/runtime/tls"
install -m 600 "$certificate_dir/fullchain.pem" "$project_dir/runtime/tls/fullchain.pem.new"
install -m 600 "$certificate_dir/privkey.pem" "$project_dir/runtime/tls/privkey.pem.new"
mv "$project_dir/runtime/tls/fullchain.pem.new" "$project_dir/runtime/tls/fullchain.pem"
mv "$project_dir/runtime/tls/privkey.pem.new" "$project_dir/runtime/tls/privkey.pem"
cd "$project_dir"
docker compose -f compose.yaml -f compose.tls.yaml exec -T nginx nginx -t
docker compose -f compose.yaml -f compose.tls.yaml exec -T nginx nginx -s reload
