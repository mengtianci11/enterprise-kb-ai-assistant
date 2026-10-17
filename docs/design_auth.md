# 设计文档：多用户体系与权限隔离（P0-③）

> 对应《企业级升级方案 v3.0》问题 3：没有多用户体系和权限隔离（谁都能传/删文档）。
> 本文档说明**方案设计、落地实现、验证证据与工程取舍**。

---

## 1. 目标与范围

### ✅ 已实现
1. **认证（AuthN）**：账号密码 + JWT（access 60min / refresh 7d），令牌校验、过期、类型校验、停用账号即时失效；
2. **授权（AuthZ）**：RBAC 四角色（admin / kb_manager / user / auditor）+ 权限点矩阵；
3. **文档级 ACL**：user / role / dept 三个维度的显式授权，密级 confidential / secret **只有显式授权才能访问**；
4. **密级标签**：public / internal / confidential / secret，与部门维度联合判定；
5. **⭐ 召回期权限过滤**：检索的两条召回路径（向量 + BM25）都在**候选生成阶段**按可见集合过滤；
6. **审计日志**：append-only，记录登录/上传/删除/问答/授权变更，含用户、IP、UA、查询原文、成功与否；
7. **多租户字段**：`tenant_id` 贯穿用户与文档，查询按租户隔离；
8. **fail-closed 迁移**：升级前无归属的历史文档划归管理员，**不会**因为引入权限而突然全员可见；
9. **前端**：登录层、令牌存储、401 自动回登录、密级选择、文档密级徽标、异步任务进度。

### 🚫 明确不在本期（以及原因）
- **企业 SSO / OIDC（Keycloak、企业微信、钉钉）**：本机无法部署第三方 IdP 做真实验证。
  已做 **SSO-ready 设计**：JWT 的 `iss`/`aud`/算法与密钥来源可替换（`get_secret()` 支持 .env/KMS 注入），
  认证成功后统一落到 `CurrentUser`，接入 SSO 只需替换「令牌换取用户」这一步。
- **bcrypt/argon2**：改用 stdlib `pbkdf2_hmac(sha256, 20 万次)`。存储串自带算法与参数
  （`pbkdf2_sha256$200000$salt$hash`），后续升级算法不需要改表；
  同时避免 Windows 上的编译依赖问题。**取舍已在代码注释中写明**。
- **pgvector/Qdrant 元数据过滤**：见第 3 节，本机无 PostgreSQL，改用「宽召回 + 白名单过滤」，
  并把过滤点收口在检索层，后续换向量库时只改 `AdvancedRAG` 内部。

---

## 2. 权限模型

### 2.1 角色 → 权限点

| 角色 | doc:read | doc:write | doc:delete | doc:acl | user:manage | audit:read | chat |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| admin | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| kb_manager | ✅ | ✅ | ✅ | ✅ | — | ✅ | ✅ |
| user | ✅ | — | — | — | — | — | ✅ |
| auditor | ✅ | — | — | — | — | ✅ | — |

### 2.2 文档可见性规则（单一事实来源：`DocumentStore.allowed_doc_ids`）

```sql
同租户 AND (
     本人上传(owner)
  OR 密级=public
  OR (密级=internal AND 部门匹配)
  OR (角色=kb_manager AND 密级 IN (internal, confidential) AND (无部门 OR 本部门))
  OR 显式 ACL 命中(user / role / dept 任一维度)
)
```
设计意图：
- **默认拒绝**（不满足任何条件即不可见）；
- **部门是内部文档的天然边界**；
- **机密/绝密只能靠显式 ACL**，避免「建个目录就全员可读」的常见事故；
- 规则集中在一条 SQL 里，**列表展示与检索过滤共用同一份逻辑**，不可能出现「列表看不到但检索能搜到」。

### 2.3 密级与授权的关系

| 密级 | 谁能看 |
| --- | --- |
| public | 同租户全员 |
| internal | 同部门 + owner + kb_manager |
| confidential | owner + 本部门 kb_manager + **显式 ACL** |
| secret | owner + **显式 ACL**（管理员可见全部，但操作留痕） |

---

## 3. ⭐ 权限过滤为什么必须发生在「召回阶段」

这是本项最核心的设计决策。行业里常见的错误实现是：

```
检索 Top-5 → 生成答案 → 输出前检查权限 → 过滤掉无权文档
```
它有三个致命问题：
1. **信息已经泄露**：无权文档的内容已经进入 LLM 上下文，可能出现在答案、引用甚至「不予显示」的措辞里；
2. **挤占候选位**：Top-5 名额被无权文档占用，**有权文档被挤出**，答案质量反而下降；
3. **不可审计**：无法证明「模型到底看到过什么」。

本项目的实现（`app/retrieval.py`）：

```python
# 向量检索：FAISS 不支持元数据过滤 → 宽召回 + 白名单过滤
pool = max(top_k, settings.RETRIEVAL_RECALL_POOL)   # 默认 60
scores, idxs = index.search(q, pool)
results = [hit for hit in ... if chunk.doc_id in allowed][:top_k]

# BM25：全量打分后必须先屏蔽无权分块，再排序
mask = np.full(len(scores), -inf); mask[allowed_idx] = scores[allowed_idx]
```

**召回池（RECALL_POOL=60）的意义**：先保证「可见文档」有足够的候选被召回，
再做融合与重排——这样权限过滤不会显著牺牲召回质量（否则过滤后可能剩不下几条）。

**权限如何传到检索层**（`app/context.py`）：Agent 的检索工具是在构建时闭包捕获 `rag` 的，
拿不到「当前请求的用户」。用 `contextvars` 承载请求级权限上下文：
```
HTTP 路由  → set_permission_context(allowed_doc_ids)   （每个请求独立副本）
Agent 工具 → current_allowed_docs.get() → rag.retrieve(..., allowed_doc_ids=...)
HTTP 路由  → reset_permission_context()                （finally 中复位）
```
好处：异步/线程安全、无需为每个用户重建 Agent（否则每次问答都要重编译 LangGraph 图）。

---

## 4. 验证证据（真实运行，非模拟）

### 4.1 单元/集成测试
`pytest`：**88 项全部通过**，其中 27 项覆盖本项能力：
- 密码哈希（盐随机、弱密码拒绝、常量时间校验）、JWT（往返、类型混淆拒绝、签名篡改拒绝）；
- 登录成功/失败、401 未认证、refresh 换发、停用账号拒登；
- RBAC：只有 admin 能管用户、user 不能上传、kb_manager 能上传但不能管用户、非法角色被拒；
- 可见性：跨部门不可见、public 全员可见、ACL 授权/撤销生效、role 维度授权生效、删除需 owner 或角色；
- **召回过滤**：检索层在给定白名单下只返回可见文档（并断言机密串不出现在结果中）；
- **HTTP 全链路**：路由设置权限上下文 → 工具读取 → 检索过滤（同一问题，权限不同结果不同）；
- 审计：登录/上传/建号被记录、失败登录被记录、auditor 只读、配置接口仅管理员可访问。

### 4.2 端到端演示（真实服务 + 真实 LLM）

| 步骤 | 结果 |
| --- | --- |
| 未认证访问 `/documents` | **401** ✅ |
| admin 创建 alice（kb_manager/销售部）、bob（user/研发部） | ✅ |
| alice 上传《销售薪酬方案.txt》（密级 confidential） | ✅ 就绪 |
| bob 查看文档列表 | **0 / 6 份可见** ✅ |
| **bob 提问「销售总监的年薪是多少？」** | 答：「知识库中没有相关记录…」**零泄露** ✅ |
| alice 授权 bob 读取该文档 | ✅ |
| **bob 再问同一问题** | 答：「销售总监的年薪为 120 万元，包含年终奖 30 万元（来源：销售薪酬方案.txt）」✅ |

> 同一个问题、同一个用户、同一个会话之外——唯一变化是权限。这是「召回期过滤」最直接的证据。

### 4.3 审计
14 条记录覆盖 `login / user_create / upload / chat / acl_change`，
含时间、用户、资源、查询原文与成功标记；`/admin/audit` 仅 admin/auditor 可读。

---

## 5. 工程取舍

| 决策 | 原因 | 代价/后续 |
| --- | --- | --- |
| 本地 JWT 而非 OIDC SSO | 本机无法部署 IdP 验证 | 接口已留 `iss`/密钥注入点，接 SSO 只改一处 |
| pbkdf2 而非 argon2/bcrypt | 零编译依赖、Windows 友好 | 抗 GPU 强度略低；已留算法升级位 |
| FAISS 宽召回+过滤 而非 pgvector | 不引入 PostgreSQL 依赖即可正确实现 | 超大规模下过滤后候选可能不足 → P2 迁 pgvector/Qdrant 原生 filter |
| contextvars 传权限 | 避免每请求重建 Agent | 需保证 finally 复位（已实现） |
| 历史文档归管理员 | fail-closed，防升级即泄露 | 需要管理员显式授权才能共享（符合预期） |
| 密钥自动生成落盘 `data/.jwt_secret` | 开箱即用、重启令牌不失效 | 生产应用 KMS/Vault 注入（README 已注明） |

---

## 6. 后续（P1/P2 与本项相关的部分）

1. 接入企业 SSO（OIDC）：把 `/auth/login` 换成授权码回调，用户信息映射到已有 `users` 表；
2. 权限变更后强制令牌失效（引入 token 版本号/jti 黑名单）；
3. 文档密级自动识别（正则/PII 检测：身份证、手机号、薪资关键词 → 自动建议密级）；
4. 行级安全下推到向量库（pgvector RLS / Qdrant filter），替代宽召回过滤；
5. 审计日志外送 SIEM、按租户分表与保留策略（≥6 个月）。
