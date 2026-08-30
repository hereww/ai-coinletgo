#!/bin/sh
set -eu

secret_dir="${SECRET_DIR:-./secrets}"
secret_uid="${SECRET_UID:-10001}"
secret_gid="${SECRET_GID:-10001}"
profile="${1:-relay}"
case "$profile" in
  relay) secret_name="model_api_key"; label="OpenAI 中转" ;;
  vllm) secret_name="vllm_model_api_key"; label="自建 vLLM" ;;
  *)
    echo '用法：scripts/configure-model-secret.sh [relay|vllm]' >&2
    exit 1
    ;;
esac
mkdir -p "$secret_dir"
umask 077
trap 'stty echo 2>/dev/null || true' EXIT INT TERM

printf '%s API Key（不会回显）： ' "$label"
stty -echo
IFS= read -r api_key
stty echo
printf '\n'

[ -n "$api_key" ] || {
  echo 'API key 不能为空' >&2
  exit 1
}

printf '%s\n' "$api_key" > "$secret_dir/$secret_name"
chmod 600 "$secret_dir/$secret_name"
if [ "$(id -u)" -eq 0 ]; then
  chown "$secret_uid:$secret_gid" "$secret_dir/$secret_name"
fi
trap - EXIT INT TERM
echo "已写入 $secret_dir/$secret_name"
echo '下一步：重建 api/worker，然后在控制台选择模型并运行结构化探针。'
