# SmartCS 可插拔 JWT 认证与会话授权设计

> 状态：已延期。本设计保留给下一轮工程化改进；当前迭代不实施登录验证、会话归属或角色授权，优先优化意图路由、RAG、工具调用与合规检查。

## 1. 背景

SmartCS 当前允许客户端直接提交 `user_id` 与 `session_id`，聊天历史、工具与指标接口也没有身份验证或角色授权。这适合本地演示，但不能用于真实客户或多租户部署。

本设计建立首版认证与授权框架，使 API 不再信任客户端自报身份，并为后续接入 OIDC/JWKS 身份平台保留稳定接口。

## 2. 目标

本批工作实现：

1. 可替换的 token 验证接口。
2. 仅用于开发和测试的 HS256 JWT 验证器。
3. `customer`、`admin`、`service` 三种角色的端点权限控制。
4. 基于 Redis 的会话归属绑定和校验。
5. 认证失败、越权和敏感管理操作的安全审计。
6. 开发环境显式匿名模式，以及生产环境 fail-closed 配置约束。
7. TUI Bearer token 支持和覆盖认证边界的自动化测试。

## 3. 非目标

本批不实现：

- 真实 OIDC/JWKS 验证器；
- 身份平台的用户注册、登录、刷新 token 或注销；
- 人工坐席 `agent` 角色；
- 租户管理后台；
- 会话正文数据库迁移；
- 工具内部业务参数的完整 JSON Schema 校验；
- 细粒度工单、订单资源授权。

这些能力在认证框架稳定后分批接入。

## 4. 已确认决策

- 生产环境所有业务接口强制认证。
- 仅显式 development/test 模式允许匿名聊天。
- 首版角色为 `customer`、`admin`、`service`。
- 客户端可以传入 `session_id`，但服务端必须校验或原子建立归属。
- 首版采用可插拔验证器和开发测试 HS256 JWT。
- 认证模式下 Redis 不可用时，会话相关接口 fail-closed，不回退到进程内归属表。
- 生产环境禁止匿名、`AUTH_MODE=disabled` 和本地 HS256 JWT。

## 5. 架构

### 5.1 模块边界

认证代码放在独立模块，避免 JWT 逻辑散落在业务端点中：

```text
api/
├── auth/
│   ├── models.py        # Principal、角色及认证异常
│   ├── verifier.py      # TokenVerifier 协议与 LocalJwtVerifier
│   ├── dependencies.py  # FastAPI 身份与角色依赖
│   ├── sessions.py      # SessionOwnershipStore
│   └── audit.py         # 安全审计事件
├── settings.py          # 环境模式与认证配置
└── main.py              # 端点组合，不实现底层认证细节
```

每个模块职责如下：

- `Principal`：可信身份模型，包含 `subject`、`roles`、`tenant_id` 和匿名标记。
- `TokenVerifier`：验证 Bearer token 并返回 `Principal` 的统一协议。
- `LocalJwtVerifier`：HS256 开发测试实现。
- `dependencies.py`：解析 Authorization Header、要求指定角色并生成统一 HTTP 错误。
- `SessionOwnershipStore`：在 Redis 中原子绑定和校验 session 归属。
- `audit.py`：输出不含敏感正文的结构化安全事件。

未来接入 OIDC 时，只新增 `JwksTokenVerifier` 并由配置选择，不改变端点与会话授权逻辑。

### 5.2 Principal 模型

`Principal` 包含：

```text
subject: str
roles: frozenset[customer | admin | service]
tenant_id: str = "default"
is_anonymous: bool = false
```

验证器必须拒绝以下 token：

- 缺少 `sub`；
- 缺少或包含未知角色；
- `exp` 已过期；
- `nbf` 尚未生效；
- issuer 或 audience 不匹配；
- 签名无效；
- 算法不是配置允许的算法。

角色不从请求体、query 或普通 Header 中读取。

## 6. 配置约束

新增配置：

```env
APP_ENV=development
AUTH_MODE=disabled
AUTH_ALLOW_ANONYMOUS=true
AUTH_LOCAL_SECRET=
AUTH_ISSUER=smartcs-local
AUTH_AUDIENCE=smartcs-api
AUTH_TOKEN_LEEWAY_SECONDS=30
```

允许的模式：

| APP_ENV | AUTH_MODE | 匿名 | 结果 |
|---|---|---:|---|
| development/test | disabled | 可显式开启 | 仅用于本地开发与测试 |
| development/test | local_jwt | 可显式开启 | 验证 HS256 token |
| production | disabled | 任意 | 配置错误，快速失败 |
| production | local_jwt | 任意 | 配置错误，快速失败 |
| production | 未实现的外部验证器 | 否 | 在后续 JWKS 批次启用 |

在当前批次中，`production` 尚无可用认证模式，因此生产启动必须失败。这是有意的安全门禁，避免将开发密钥误用于生产。

`AUTH_LOCAL_SECRET`：

- 仅在 `local_jwt` 模式读取；
- 不提供默认值；
- 不写入真实 `.env.example` 值；
- 不出现在日志、异常和 metrics；
- 要求足够长度，短密钥直接拒绝启动。

## 7. 权限矩阵

| 接口 | customer | admin | service | 匿名 |
|---|---:|---:|---:|---:|
| `/health`、`/ready` | 是 | 是 | 是 | 是 |
| `/api/chat` | 自己 | 是 | 否 | 仅显式开发模式 |
| `/api/history/{session_id}` | 仅自己的 | 是 | 否 | 否 |
| `/api/tools` | 否 | 是 | 是 | 否 |
| `/api/tools/call` | 否 | 否 | 是 | 否 |
| `/api/metrics` | 否 | 是 | 否 | 否 |

`admin` 可以查看任意会话历史，但必须生成安全审计事件。`service` 只用于服务间工具调用，不允许冒充客户聊天。

## 8. 请求身份兼容策略

`ChatRequest.user_id` 在首版继续保留，以避免一次性破坏 TUI 和现有客户端，但它不再是可信身份来源：

- 已认证请求中，实际 `user_id` 始终使用 `Principal.subject`。
- 若请求体 `user_id` 不是兼容默认值且与 `subject` 不一致，返回 `403 USER_ID_MISMATCH`。
- 匿名开发请求忽略请求体中试图指定其他用户的值，使用服务器生成的匿名 subject。
- 后续 API 大版本删除 `user_id` 字段。

## 9. 会话归属

### 9.1 Redis 数据模型

Redis key：

```text
smartcs:session_owner:{session_id}
```

value 使用版本化 JSON：

```json
{
  "schema_version": 1,
  "subject": "user-123",
  "tenant_id": "default",
  "created_at": "2026-07-11T00:00:00Z"
}
```

归属 TTL 至少与会话正文 TTL 一致；每次有效聊天时同步续期。

### 9.2 新会话

用户未提供 `session_id` 时：

1. 服务端生成 UUID；
2. 使用 Redis `SET key value NX EX ttl` 原子绑定；
3. 绑定成功后再保存用户消息；
4. 极低概率冲突时重新生成并有限重试。

### 9.3 客户端指定 session

已认证客户提交 `session_id` 时：

- key 已存在且 `subject + tenant_id` 一致：允许访问并续期；
- key 已存在但归属不同：返回 `403 SESSION_FORBIDDEN`；
- key 不存在：使用 `SET NX` 将其绑定为新会话；
- 并发绑定失败：重新读取归属并按上面规则判断。

管理员读取历史可以绕过归属，但要记录审计。管理员发起聊天时仍需明确目标会话，且本批不提供代客操作能力，避免扩大权限。

### 9.4 匿名会话

开发匿名聊天使用服务器生成的临时 subject：

```text
anonymous:<random-id>
```

匿名请求：

- 只能创建新聊天会话；
- 不能读取历史；
- 不能调用工具或指标接口；
- 不能携带已有 `session_id` 接管会话。

### 9.5 Redis 故障

认证或会话归属开启后，Redis 无法读写时：

- `/api/chat` 和 `/api/history/*` 返回 `503 SESSION_OWNERSHIP_UNAVAILABLE`；
- 不使用进程内归属表；
- `/ready` 报告 not ready；
- 记录不含 token 和正文的错误审计。

这保证多实例环境不会因降级造成跨用户访问。

## 10. 请求数据流

### 10.1 已认证聊天

```text
Authorization Bearer token
→ TokenVerifier
→ Principal
→ 角色检查 customer/admin
→ user_id 一致性检查
→ SessionOwnershipStore 绑定/校验
→ 写入消息
→ LangGraph
→ 返回响应
```

任何认证或归属失败均发生在消息写入与 Agent/工具调用之前。

### 10.2 工具调用

```text
Authorization Bearer token
→ TokenVerifier
→ 要求 service 角色
→ 工具 allowlist/参数信封校验
→ 记录审计开始事件
→ MCPToolServer
→ 记录成功/失败审计结果
```

本批建立端点级工具权限；工具级 allowlist 和业务资源授权将在后续副作用安全批次细化。

## 11. 错误模型

统一错误语义：

| HTTP | code | 场景 |
|---:|---|---|
| 401 | `AUTH_REQUIRED` | 缺少 Bearer token |
| 401 | `TOKEN_INVALID` | 签名、格式、issuer、audience 等无效 |
| 401 | `TOKEN_EXPIRED` | token 已过期 |
| 403 | `ROLE_FORBIDDEN` | 身份有效但角色不足 |
| 403 | `USER_ID_MISMATCH` | 请求体 user_id 与 token subject 不一致 |
| 403 | `SESSION_FORBIDDEN` | session 属于其他身份或租户 |
| 503 | `AUTH_NOT_CONFIGURED` | 认证模式配置不安全或缺少验证器 |
| 503 | `SESSION_OWNERSHIP_UNAVAILABLE` | Redis 无法完成归属检查 |

错误响应包含稳定 `code` 和 `request_id`，不返回 token、claim、密钥、连接地址、异常正文或堆栈。

## 12. 安全审计

首版记录以下事件：

- token 验证失败；
- 角色拒绝；
- user_id 不一致；
- session 归属拒绝；
- 管理员读取客户会话；
- service 工具调用开始与结果；
- 会话归属存储不可用。

事件字段：

```text
timestamp
request_id
action
result
subject_hash
roles
resource_id
tenant_id
reason_code
```

`subject_hash` 使用应用级不可逆摘要；日志禁止记录 JWT、消息正文、工具敏感参数、密钥及完整用户标识。

## 13. Swagger 与运维端点

- `/health`、`/ready` 保持匿名，用于平台探针。
- 开发/test 保留 `/docs`、`/openapi.json`。
- production 默认禁用 `/docs`、`/redoc` 和公开 OpenAPI。
- `/api/metrics` 要求 `admin`；后续迁移为内网 Prometheus 端点。

## 14. TUI 兼容

TUI 增加：

```text
--token <JWT>
SMARTCS_API_TOKEN=<JWT>
```

命令行参数优先于环境变量。客户端发送：

```http
Authorization: Bearer <token>
```

禁止在异常、调试输出或历史中显示 token。开发匿名模式下可以不提供 token，但 `/history` 会返回 401。

## 15. 测试策略

### 15.1 配置测试

- production 拒绝 disabled；
- production 拒绝 anonymous；
- production 拒绝 local JWT；
- local JWT 缺少或使用短密钥时失败；
- development/test 只有显式配置才允许匿名。

### 15.2 JWT 单元测试

- 有效签名和 claims；
- 无效签名；
- 过期和尚未生效；
- issuer/audience 不匹配；
- 缺失 subject/roles；
- 未知角色；
- 算法降级攻击被拒绝。

### 15.3 权限矩阵测试

对每个受保护端点覆盖：

- 缺 token；
- customer；
- admin；
- service；
- 开发匿名。

### 15.4 会话测试

- 首次绑定；
- 本人重复访问；
- 他人访问被拒绝；
- 不同租户同 subject 被拒绝；
- 并发 `SET NX` 只有一个主体成功；
- 管理员读取产生审计；
- Redis 故障时 fail-closed。

### 15.5 回归测试

- 保留现有 60 个测试；
- 测试默认使用 `APP_ENV=test`；
- 更新 API/TUI fixtures 注入 token；
- 不依赖真实身份服务或真实 Redis，Redis 归属测试使用明确的测试替身覆盖原子语义。

## 16. 验收标准

1. 开启认证后，缺少或无效 token 的业务接口返回 401。
2. 客户无法查看其他 subject 或 tenant 的会话。
3. `customer` 无法访问工具与指标接口。
4. `admin` 能访问指标，读取会话时生成审计事件。
5. 只有 `service` 能调用 `/api/tools/call`。
6. 请求体伪造 `user_id` 无法改变可信身份。
7. Redis 故障时不会回退到不安全的进程内归属存储。
8. production 配置不能启用匿名、disabled 或 local JWT。
9. 日志和错误响应中没有 token、密钥、消息正文和内部异常。
10. TUI 可以安全发送 Bearer token，且不输出 token。
11. 新增测试与现有回归测试全部通过。

## 17. 后续演进

下一批认证工作新增 `JwksTokenVerifier`，支持 OIDC issuer discovery、JWKS 缓存和 key rotation。由于端点仅依赖 `TokenVerifier` 和 `Principal`，迁移时不改变权限矩阵、会话归属或业务 Agent。