#!/bin/sh
set -eu

secret_dir="${SECRET_DIR:-./secrets}"
mkdir -p "$secret_dir"
umask 077
trap 'stty echo 2>/dev/null || true' EXIT INT TERM

printf 'Binance U 本位测试网 API Key（不会回显）： '
stty -echo
IFS= read -r api_key
stty echo
printf '\n'
printf 'Binance U 本位测试网 API Secret（不会回显）： '
stty -echo
IFS= read -r api_secret
stty echo
printf '\n'

[ -n "$api_key" ] && [ -n "$api_secret" ] || {
  echo 'API key/secret 不能为空' >&2
  exit 1
}

printf '%s\n' "$api_key" > "$secret_dir/binance_testnet_api_key"
printf '%s\n' "$api_secret" > "$secret_dir/binance_testnet_api_secret"
chmod 600 "$secret_dir/binance_testnet_api_key" "$secret_dir/binance_testnet_api_secret"
trap - EXIT INT TERM
echo "已写入 $secret_dir/binance_testnet_api_key 和 binance_testnet_api_secret"
echo '下一步：重建 api/worker，或在控制台点击“测试 Binance 测试网连接”。'
