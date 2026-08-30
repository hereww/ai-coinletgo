#!/usr/bin/env bash
set -euo pipefail

secret_dir="${SECRET_DIR:-./secrets}"
env_file="${ENV_FILE:-./.env}"
mkdir -p "$secret_dir"
chmod 700 "$secret_dir"

set_enabled() {
  value="$1"
  if [ -f "$env_file" ]; then
    if grep -q '^HTTP_PROXY_ENABLED=' "$env_file"; then
      sed -i "s/^HTTP_PROXY_ENABLED=.*/HTTP_PROXY_ENABLED=$value/" "$env_file"
    else
      printf '\nHTTP_PROXY_ENABLED=%s\n' "$value" >> "$env_file"
    fi
  fi
}

if [ "${1:-}" = "--disable" ]; then
  set_enabled false
  echo '已关闭 Binance + AI 中转 HTTP 代理模式；已有 secret 保留，便于再次启用。'
  echo '下一步：docker compose -f docker-compose.server.yml up -d --build api worker'
  exit 0
fi

printf 'HTTP/HTTPS 代理地址（不会回显，例如 http://user:pass@host:port）： '
IFS= read -r -s proxy_url
printf '\n'
case "$proxy_url" in
  http://*|https://*) ;;
  *) echo '代理地址必须以 http:// 或 https:// 开头' >&2; exit 1 ;;
esac

printf '%s\n' "$proxy_url" > "$secret_dir/http_proxy_url"
chmod 600 "$secret_dir/http_proxy_url"

set_enabled true

echo "已写入 $secret_dir/http_proxy_url，并启用 Binance + AI 中转代理模式。"
echo '下一步：docker compose -f docker-compose.server.yml up -d --build api worker'
