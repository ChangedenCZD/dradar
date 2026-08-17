#!/usr/bin/env bash
set -euo pipefail

: "${PROXY_TOKEN:?PROXY_TOKEN is required}"
: "${ALLOWLIST_DOMAINS:?ALLOWLIST_DOMAINS is required}"

umask 077
allowed_domains=/tmp/allowed_domains.txt
password_file=/tmp/squid.passwd

printf '%s' "$ALLOWLIST_DOMAINS" \
  | tr ',' '\n' \
  | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;/^$/d' \
  > "$allowed_domains"

if [[ ! -s "$allowed_domains" ]]; then
  echo "ALLOWLIST_DOMAINS did not contain any domains" >&2
  exit 2
fi

while IFS= read -r domain; do
  if ! printf '%s\n' "$domain" | grep -Eq '^\.?[A-Za-z0-9][A-Za-z0-9._:-]*$'; then
    echo "ALLOWLIST_DOMAINS contains an invalid domain" >&2
    exit 2
  fi
done < "$allowed_domains"

# -i reads the password from stdin, keeping the short-lived credential out of
# process arguments. Squid only needs the resulting htpasswd file.
printf '%s\n' "$PROXY_TOKEN" | htpasswd -ci "$password_file" agent >/dev/null
unset PROXY_TOKEN

upstream_config=
if [[ -n "${UPSTREAM_PROXY_HOST:-}" ]]; then
  if ! printf '%s\n' "$UPSTREAM_PROXY_HOST" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._:-]*$'; then
    echo "UPSTREAM_PROXY_HOST is invalid" >&2
    exit 2
  fi
  if ! printf '%s\n' "${UPSTREAM_PROXY_PORT:-}" | grep -Eq '^[0-9]{1,5}$' \
      || (( UPSTREAM_PROXY_PORT < 1 || UPSTREAM_PROXY_PORT > 65535 )); then
    echo "UPSTREAM_PROXY_PORT is invalid" >&2
    exit 2
  fi
  upstream_config="cache_peer ${UPSTREAM_PROXY_HOST} parent ${UPSTREAM_PROXY_PORT} 0 no-query default"
  if [[ -n "${UPSTREAM_PROXY_USERNAME:-}" || -n "${UPSTREAM_PROXY_PASSWORD:-}" ]]; then
    if [[ "${UPSTREAM_PROXY_USERNAME:-}" == *[[:space:]#]* \
        || "${UPSTREAM_PROXY_PASSWORD:-}" == *[[:space:]#]* ]]; then
      echo "upstream proxy credentials contain unsupported characters" >&2
      exit 2
    fi
    upstream_config+=" login=${UPSTREAM_PROXY_USERNAME:-}:${UPSTREAM_PROXY_PASSWORD:-}"
  fi
  upstream_config+=$'\nnever_direct allow all'
fi

cat > /tmp/squid.conf <<'EOF'
http_port 0.0.0.0:8080
pid_filename /tmp/squid.pid
coredump_dir /tmp

auth_param basic program /usr/lib/squid/basic_ncsa_auth /tmp/squid.passwd
auth_param basic realm PierPolicyProxy
acl authenticated proxy_auth REQUIRED

acl SSL_ports port 443
acl Safe_ports port 80 443
acl CONNECT method CONNECT
acl allowed_domains dstdomain "/tmp/allowed_domains.txt"

http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
http_access allow authenticated allowed_domains
http_access deny all

cache deny all
access_log stdio:/tmp/squid_access.log
cache_log /tmp/squid_cache.log
log_mime_hdrs off
shutdown_lifetime 1 seconds
EOF

if [[ -n "$upstream_config" ]]; then
  printf '\n%s\n' "$upstream_config" >> /tmp/squid.conf
fi

exec squid -N -f /tmp/squid.conf -d 1
