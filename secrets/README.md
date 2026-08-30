# Secret files

Compose mounts only the specific secret files each service needs at `/run/secrets`; no service receives the full secret directory. All files except this README are ignored by Git. Store one raw value per file and apply `chmod 600`. The API and worker containers run as UID/GID `10001`; when secret files are created by root, set their owner to `10001:10001` so the processes can read them. The model-secret helper does this automatically.

Required files include `postgres_password`, `backup_encryption_key`, `binance_testnet_api_key`, `binance_testnet_api_secret`, `binance_live_api_key`, `binance_live_api_secret`, `model_api_key`, `vllm_model_api_key`, `auth_password_hash`, and `session_secret`. `http_proxy_url` is optional; when `HTTP_PROXY_ENABLED=true`, it is shared by Binance and the active AI model. Telegram files are optional when notifications are disabled.

Never put the PostgreSQL password in `.env` or `DATABASE_URL`; services add the URL-encoded value from `postgres_password` at runtime. `session_secret` must contain at least 32 characters in production. A suitable value can be generated with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`.
