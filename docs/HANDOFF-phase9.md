# 交接指令：服务启动（用户验收用）+ Compose 容器化

> **发件方**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04 ｜ **优先级：服务启动最优先**
> **前置**：迁移已终局验收、分层提交完成（python-impl 分支 feat/pi-harness-migration，pi-harness 独立仓）。

---

## 第 1 部分（立即执行）：启动双层服务供用户验收

### 目标状态

```
MySQL(:3307, 已在跑) + Redis(已在跑)
  ↑
Python api.main（uvicorn，公开端点 + web 工作台）
  ↑ 转发 pi 会话
pi-harness Node 服务（SMARTCS_PI_ROLLOUT_PERCENT=100，新会话全走 pi）
```

### 步骤

1. **迁移 dev 库**：对 `smartcs_checkpoint` 依序执行 `migrations/001~004`（幂等、纯增量——加列与新表，legacy 行为零影响）。执行前备份该库（mysqldump 到临时文件），报告备份路径。
2. **准备环境变量**（进程级传入优先；如需持久便利可追加到 `.env`——本地 gitignored 文件，追加须注释清晰并在报告列出）：
   - Python 侧：`SMARTCS_PI_ROLLOUT_PERCENT=100`、`PI_HARNESS_BASE_URL=http://127.0.0.1:8971`
   - pi-harness 侧：`INTERNAL_SERVICE_JWT_SECRET`（生成 ≥32B 随机值，两侧一致）、`SMARTCS_RUNTIME_CWD`、`SMARTCS_PI_SESSION_DIR`（持久目录，如 `pi-harness/.runtime/pi-sessions`）、`SMARTCS_WRITE_MODE=live`（真实写链路开张——验证退款两段式）、`SMARTCS_SKILLS=on`（用户可体验技能披露）
3. **启动两个服务**（后台进程，记录 PID 与日志文件路径）。
4. **冒烟自验**（交付前必做）：
   - 两服务 health 端点 200
   - 用一个**新建测试账号**走通：注册/登录 → 新会话 → chat 一轮（应命中 pi 路径：响应带 `harness_version="pi"`、receipt completed）
   - 再走一轮**知识检索**（RAG）与一轮**订单查询**（真工具）
   - 确认 legacy 会话（若存在旧会话）仍走旧链路
5. **向我汇报**（跨会话 SendMessage）：web 工作台 URL、测试账号的用户名/密码、各服务端口与日志路径、冒烟结果摘要。

### 约束

- **不要动 git**（不 commit/push，含 pi-harness 仓）。
- 服务进程用 nohup/后台方式常驻；记录启动命令以便复现。
- 若 dev 库中无可用测试账号，创建一个 demo 账号（Argon2 哈希入库）并报告明文凭证（仅本地验收用）。
- 冒烟不过不许交——红了如实报 blocked。

## 第 2 部分（服务稳定后执行）：Compose 容器化

1. `pi-harness/Dockerfile`（node 镜像 + `npm ci` + tsx 运行时 + 非 root 用户 + healthcheck）
2. `pi-harness/compose.yaml`（或在 python-impl/compose.yaml 注册 pi-harness 服务——**python-impl/compose.yaml 本次授权修改**，最小 diff）：环境变量接线、`SMARTCS_PI_SESSION_DIR` 持久卷、depends_on、网络
3. 验证：`docker compose up` 拉起全套（api/redis/mysql/pi-harness），容器内冒烟（health + 一轮 pi chat）
4. 交付：新增 Dockerfile/compose 变更 + 容器化冒烟证据，追加报告 `pi-harness/PHASE9_REPORT.md`（服务启动记录 + 容器化），终行 `PHASE9_DONE <状态词>`。

### 约束（第 2 部分）

- 容器化**不得改变本地进程启动方式**（两种部署形态并存）。
- 仍不 commit；docker compose 配置属新工作，验收后统一提交。
- MySQL :3307 容器与 compose.checkpoint.yaml 的关系保持现状，不重构既有部署。
