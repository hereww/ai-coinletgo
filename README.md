# 合约风控台

当前版本：**测试跑通版本 1.0**（内部语义版本 `1.0.0`，定义于 2026-09-05）。

面向个人专用币安子账户的 U 本位合约自动交易系统。系统把市场筛选、模型判断、硬风控和订单执行分离；模型不能直接访问币安，也不能修改仓位大小、杠杆或熔断状态。

> 自动化交易存在本金损失风险。本项目不承诺盈利。首次接入必须使用币安测试网，完成账户模式、部分成交、保护单和故障恢复验收后再考虑实盘。

## 组成

- `backend/`：FastAPI、交易 Worker、量化筛选、模型契约、硬风控、币安适配器和审计存储。
- `frontend/`：React + TypeScript 响应式控制台。
- `docker-compose.yml`：PostgreSQL、Redis、API、Worker、Web、Caddy 和本机备份。
- `secrets/`：本机密钥挂载目录，内容被 Git 忽略。

## HTTP 代理模式

如果 VPS 直连 Binance 或 AI 中转不稳定，可启用统一 HTTP/HTTPS 出站代理。代理地址只写入
`secrets/http_proxy_url`，不进入数据库、前端或日志；设置 `HTTP_PROXY_ENABLED=true` 后，
Binance REST/WebSocket 和 AI Responses API 会共同使用该代理。服务器上可运行：

```bash
./scripts/configure-http-proxy.sh
docker compose -f docker-compose.server.yml up -d --build api worker
```

代理模式只支持 HTTP/HTTPS 代理地址，不会自动读取宿主机的 `HTTP_PROXY` 环境变量；未启用时
客户端强制直连，避免部署环境的隐式代理改变交易链路。

## 本地开发

需要 Python 3.12、Node.js 20+、PostgreSQL 和 Redis。也可以直接使用 Docker Compose。

```bash
cp .env.example .env
docker compose up --build
```

打开 `http://localhost:8080`。默认开发配置关闭认证；没有接入币安时显示空账户，不会生成模拟仓位或收益。实盘状态始终锁定。

分别启动：

```bash
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
uvicorn trading_system.main:app --reload --port 8000

cd frontend && corepack enable && pnpm install
pnpm run dev
```

本地 Vite 默认把 `/api` 代理到 `http://localhost:8000`。如果后端运行在 VPS，启动前设置
`VITE_DEV_API_TARGET=https://192.168.100.249:18443 pnpm run dev`，浏览器仍从本地同源访问，
不会因为生产 Secure/SameSite Cookie 被跨域请求拦截。生产环境使用 Caddy 同源地址，不需要设置
`VITE_API_BASE_URL`。

## 生产密钥

不要把任何密钥粘贴到聊天、提交到 Git 或写入数据库。将以下文件以 `chmod 600` 放入 `secrets/`：

- `binance_testnet_api_key`、`binance_testnet_api_secret`
- `binance_live_api_key`、`binance_live_api_secret`
- `model_api_key`
- `auth_password_hash`、`session_secret`
- `telegram_bot_token`、`telegram_chat_id`
- `postgres_password`、`backup_encryption_key`

`postgres_password` 只能写入 `secrets/postgres_password`，不要把密码拼入 `DATABASE_URL` 或写入 `.env`。API、Worker 和 Alembic 会从只读 secret 读取密码并进行 URL 编码。

生产环境必须设置 `AUTH_REQUIRED=true`、`COOKIE_SECURE=true`，并配置固定域名。`session_secret` 至少 32 个字符，建议使用以下命令生成后写入 `secrets/session_secret`：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

币安 Key 只允许读取和合约交易，禁用提现并绑定 VPS 固定 IP。

生成 Argon2id 密码哈希时在本机执行：

```bash
cd backend
.venv/bin/python -c 'from argon2 import PasswordHasher; import getpass; print(PasswordHasher().hash(getpass.getpass()))'
```

将输出写入 `secrets/auth_password_hash`。敏感操作统一只使用该操作密码验证。

测试网和模型中转密钥可在服务器项目目录通过交互式脚本写入，输入不会回显：

```bash
./scripts/configure-testnet-secrets.sh
./scripts/configure-model-secret.sh relay
./scripts/configure-model-secret.sh vllm
docker compose -f docker-compose.server.yml up -d --build api worker
```

控制台“设置”页提供可切换的模型配置列表。OpenAI 中转使用 `model_api_key`，自建 vLLM
使用 `vllm_model_api_key`；Base URL、模型名、推理强度、超时和预算等非秘密参数由服务器或
运行时配置管理。API key 永远不通过浏览器提交，也不写入数据库。结构化输出探针始终测试
当前选中的模型。

总览页的“立即分析并执行”会唤醒 Worker 运行一轮完整流程：市场筛选 → AI 判断 → 硬风控 →
限价入场 → 交易所保护单。它不是人工下单接口，不接受手填数量、杠杆或市价指令；如果 AI
没有合格信号或硬风控拒绝，本轮不会开仓。

## 安全门禁

实盘解锁接口要求：

- 密码会话与操作密码验证通过；
- 币安时间、账户和订单对账正常；
- Responses API 真实结构化输出探针通过；
- 市场数据、用户数据流、数据库和 Redis 健康；
- 独立交易 Worker 心跳未过期，且 Binance 用户数据流当前处于连接状态；
- API Key 可进行合约交易，账户为双向模式，现有仓位全部逐仓且不高于当前风控配置（最高 30 倍）；
- 数据库仓位、币安仓位和交易所端硬止损完全一致；
- 所有必需密钥来自只读 secret 文件。

缺少任何条件时，系统保持 `LIVE_LOCKED`。模型、行情或账户流故障时不允许新开仓，已有仓位继续由交易所端保护单和本地状态机管理。

`RECONCILIATION_REQUIRED` 不能通过普通恢复操作绕过。只能在操作密码验证后完成全部已保护仓位的对账，或执行紧急清仓。切换测试网/实盘运行环境也会自动进入该状态。

## 测试网验收

代码中的模拟适配器测试不能替代币安官方测试网。配置测试网 Key 后，至少人工验证以下场景并检查订单与审计记录：

1. 双向模式和逐仓 1–30 倍杠杆设置；提高杠杆时仍验证单笔风险、组合风险和保证金上限不会被绕过。
2. 限价单未成交、部分成交、撤单和最多两次重挂。
3. 每次新增成交量在一秒轮询内建立硬止损；保护失败时立即市价平仓并暂停。
4. 1R/2R 分批止盈、费用保本止损和最后 20% 的 1.5 ATR 跟踪。
5. Worker 重启、用户数据流断线、时钟漂移和仓位不一致。
6. 日亏损、回撤熔断、密码验证恢复、仓位对账和紧急清仓。

没有完成上述官方测试网验收前，不应把 `BINANCE_ENVIRONMENT` 切换为 `live`。

## 历史回放口径

多合约回放使用同一账户净值和保证金池，逐个候选调用与实盘相同的 `RiskEngine`，因此会执行单笔/组合风险、最大仓位数、同向仓位数、30 天 1 小时收益率相关性、保证金占用、日亏损和回撤熔断。系统会额外加载开始日期前 30 天的 K 线作为预热数据；预热区间只用于指标和相关性，不计入回放收益。

回放使用币安历史资金费率，并使用任务运行时读取到的交易所 `tickSize`、`stepSize`、最小数量和最小名义价值进行取整及拒绝判断。币安标准接口不提供任意历史时点的完整合约 filters 快照，因此结果会保存本次使用的 filters，不能把它解释为对历史规则变化的精确还原。盘口深度、历史点差、基差和持仓量目前仍使用保守占位值，回放结果只用于研究，不是实盘收益承诺。

## 验证

```bash
cd backend && ruff check src tests && mypy src/trading_system && pytest
cd frontend && pnpm run lint && pnpm run test && pnpm run build
docker compose config
```

部署后可运行不包含密码明文参数的 smoke test：

```bash
SMOKE_BASE_URL=https://192.168.100.249:18443 \
FRC_SMOKE_PASSWORD='<从密码管理器读取>' SMOKE_INSECURE=true \
./scripts/smoke-server.sh
```

PostgreSQL 由 Alembic 管理结构升级。备份容器每天生成 AES-256-CBC 加密备份，保留 30 天日备份和约 12 个月月备份。密钥文件只读挂载且不进入备份。第一版没有外部心跳，因此整台 VPS 断网或宕机时无法主动发送 Telegram 告警，交易所端已有保护单仍然有效。

恢复演练必须在隔离 PostgreSQL 执行：`/scripts/restore-postgres.sh /backups/daily/<file> CONFIRM_RESTORE`。脚本会先验证解密和压缩完整性，再以 `ON_ERROR_STOP=1` 导入 SQL；校验失败时不会修改数据库。
