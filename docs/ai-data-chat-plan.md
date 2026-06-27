# AI 数据查询对话框方案

> 目标：在 TG Monitor Platform 中增加一个 AI 对话框，用户/管理员可以用自然语言询问当前数据中的问题，由 AI 自主决定查什么表、用什么 SQL、统计什么指标、限定什么时间范围，并返回答案。

---

## 1. 结论与选型建议

### 1.1 pi.dev 是否适合引入？

**不建议直接引入。**

- pi.dev（GitHub: `earendil-works/pi`）是 **TypeScript/Node.js** 终端 AI 编程助手，核心运行时 `pi-agent-core` 是 Node 包。
- 当前项目是 **Python + FastAPI + SQLAlchemy**，没有 Node 运行时。
- 若强行引入，需要用子进程/RPC 再包一层 Node 服务，架构复杂、维护成本高，收益不明显。

### 1.2 推荐方案：Pydantic AI

| 维度 | Pydantic AI | LangGraph | OpenAI Function Calling |
|------|-------------|-----------|-------------------------|
| 与现有栈契合度 | ⭐⭐⭐⭐⭐ 同生态 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| 实现 Loop 引擎 | ✅ Tool calling + Agent loop | ✅ 状态机图 | ✅ 手写 loop |
| 依赖量 | 中等 | 较大 | 最小 |
| 复杂工作流支持 | 中等 | 最强 | 需自研 |
| 推荐度 | **首选** | 复杂场景升级 | 极简场景 |

**最终建议：**

- **MVP 阶段**：使用 `pydantic-ai` 实现"自然语言 → SQL → 结果 → 中文回答"的 Agent 循环。
- **进阶阶段**：如需要多步推理、人工审批、错误重试流水线，可升级到 `LangGraph`。

---

## 2. 功能定位

### 2.1 谁可以用？

| 角色 | 权限 | 说明 |
|------|------|------|
| 管理员 | 全部数据 | 可问系统级、跨群、敏感指标 |
| 普通用户 | 受限数据 | 仅能看到其有权限的群组/指标（可选） |

### 2.2 可以问什么？

示例：

- "过去 24 小时哪个群消息最多？"
- "最近 7 天有哪些新的 Key 商线索？"
- "商品 X 的价格趋势如何？"
- "昨天提到最多的前 10 个关键词是什么？"
- "用户 A 最近活跃吗？发了什么内容？"
- "有没有群里在讨论 free credits？"

### 2.3 AI 需要自主决定什么？

1. **查什么表**：`messages`、`ai_key_leads`、`ai_products`、`daily_chat_stats` 等。
2. **用什么 SQL/聚合函数**：SELECT、GROUP BY、COUNT、SUM、JOIN、时间过滤。
3. **什么时间范围**：默认最近 7 天 / 24 小时，用户明确时按用户要求。
4. **是否需要多步查询**：先查总数，再查 TOP 10，再下钻详情。
5. **如何组织答案**：中文总结 + 关键数字 + 表格/列表。

---

## 3. 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│                        前端 (Browser)                        │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐   │
│  │ 浮动对话按钮  │  │  消息气泡    │  │  数据源引用     │   │
│  └──────────────┘  └──────────────┘  └─────────────────┘   │
└──────────────────────┬──────────────────────────────────────┘
                       │ POST /api/chat 或 SSE /api/chat/stream
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     FastAPI (app/web.py)                     │
│  ┌───────────────────────────────────────────────────────┐  │
│  │              AI 数据查询 Agent (app/ai_chat.py)        │  │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────┐  │  │
│  │  │ 意图理解    │ │ SQL 生成    │ │ 结果解释        │  │  │
│  │  └─────────────┘ └─────────────┘ └─────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │ Tool Registry:                                  │  │  │
│  │  │ • get_schema()      获取可用表结构              │  │  │
│  │  │ • run_sql()         执行只读 SQL                │  │  │
│  │  │ • get_chat_list()   获取用户有权限的群列表      │  │  │
│  │  │ • get_date_range()  解析时间范围                │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────────────┬──────────────────────────────────────┘
                       │ 只读连接 / ORM
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     MySQL (现有数据库)                        │
│         messages / ai_* / daily_chat_stats / ...            │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 数据流（核心 Loop）

```
用户提问
   │
   ▼
┌─────────────────┐
│ 1. 安全校验     │  认证、权限、SQL 注入预检
└────────┬────────┘
         ▼
┌─────────────────┐
│ 2. 构建 Prompt  │  注入表结构、可用工具、时间规则
└────────┬────────┘
         ▼
┌─────────────────┐
│ 3. LLM 思考     │  "我要查 messages 表，按 chat_id 分组，
│                 │   统计最近 24 小时消息数"
└────────┬────────┘
         ▼
┌─────────────────┐
│ 4. 工具调用     │  run_sql("SELECT chat_id, COUNT(*) ...")
└────────┬────────┘
         ▼
┌─────────────────┐
│ 5. 获取结果     │  返回行数据
└────────┬────────┘
         │
         ▼ 数据不够？
    ┌─────────┐
    │  继续    │ ──▶ 回到步骤 3，生成下一条 SQL
    │  查询？  │
    └────┬────┘
         │ 足够
         ▼
┌─────────────────┐
│ 6. 生成中文答案 │  总结 + 数字 + 表格
└────────┬────────┘
         ▼
┌─────────────────┐
│ 7. 返回答案     │  JSON / SSE 流式
└─────────────────┘
```

---

## 5. 关键模块设计

### 5.1 新增文件

| 文件 | 职责 |
|------|------|
| `app/ai_chat.py` | Agent 核心：prompt、tool registry、loop 调用 |
| `app/ai_chat_tools.py` | 工具函数：`run_sql`、`get_schema`、`get_chat_list` 等 |
| `app/ai_chat_prompts.py` | 系统 prompt、few-shot 示例、表结构描述生成 |
| `app/models_chat.py` | 新增 `chat_sessions`、`chat_messages` 表模型 |
| `app/web.py` | 新增 `/chat` 页面路由 + `/api/chat` 接口 |
| `app/templates/chat.html` | 对话框页面 |
| `app/static/chat.js` | 前端交互（发送消息、流式展示、数据源折叠） |

### 5.2 数据库新增表（可选但推荐）

```sql
CREATE TABLE chat_sessions (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id VARCHAR(64) NOT NULL,
    title VARCHAR(255),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user (user_id, updated_at)
);

CREATE TABLE chat_messages (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    session_id BIGINT NOT NULL,
    role ENUM('user','assistant','tool') NOT NULL,
    content TEXT,
    tool_name VARCHAR(64),
    tool_input TEXT,
    tool_output TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
    INDEX idx_session (session_id, created_at)
);
```

### 5.3 Agent Prompt 设计要点

```text
你是 TG Monitor 平台的数据分析助手。你只能通过只读 SQL 查询数据库来回答用户问题。

可用表：
- messages(chat_id, sender_id, message_date, raw_text, normalized_text, ...)
- ai_key_leads(chat_id, sender_id, confidence, last_seen_at, ...)
- ai_products(name, price, currency, seller, status, discovered_at, ...)
- daily_chat_stats(chat_id, stat_date, message_count, active_user_count, ...)
- telegram_users(id, username, first_name, about, ...)

规则：
1. 只能生成 SELECT 语句，禁止 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE。
2. 如果用户没有指定时间，默认查询最近 7 天。
3. 涉及时间字段使用 message_date >= DATE_SUB(NOW(), INTERVAL 7 DAY)。
4. 查询结果超过 100 行时要 LIMIT 100 并提醒用户。
5. 最终答案用中文，包含关键数字和简短结论。

你可以调用以下工具：
- run_sql(sql): 执行 SQL 并返回结果
- get_schema(): 获取完整表结构
- get_chat_list(): 获取当前用户有权限的群
```

### 5.4 Tool 设计（Pydantic AI 风格伪代码）

```python
from dataclasses import dataclass
from pydantic_ai import Agent, RunContext
from sqlalchemy import text

@dataclass
class ChatDeps:
    db: Session
    schema: str
    user_role: str
    allowed_chat_ids: list[int]


agent = Agent(
    model='openai:gpt-4o-mini',  # 实际从 app_settings 读取
    deps_type=ChatDeps,
    result_type=str,
    system_prompt=build_system_prompt(),
)


@agent.tool
async def run_sql(ctx: RunContext[ChatDeps], sql: str) -> str:
    """执行只读 SQL，返回 JSON 化结果。"""
    sql = sql.strip()
    if not sql.upper().startswith('SELECT'):
        return '错误：只能执行 SELECT 查询。'
    try:
        rows = ctx.deps.db.execute(text(sql)).mappings().all()
        return json.dumps([dict(r) for r in rows[:100]], ensure_ascii=False, default=str)
    except Exception as e:
        return f'执行失败：{e}'


@agent.tool
async def get_schema(ctx: RunContext[ChatDeps]) -> str:
    """返回数据库表结构描述。"""
    return ctx.deps.schema


async def ask(question: str, user: UserContext) -> str:
    db = SessionLocal()
    try:
        deps = ChatDeps(
            db=db,
            schema=load_schema_description(),
            user_role=user.role,
            allowed_chat_ids=user.allowed_chat_ids,
        )
        result = await agent.run(question, deps=deps)
        return result.output
    finally:
        db.close()
```

---

## 6. 前端 UI 设计

### 6.1 两种形态（二选一）

| 形态 | 优点 | 缺点 |
|------|------|------|
| **A. 全局浮动气泡** | 任何页面都能问，随时可用 | 空间小，复杂答案展示受限 |
| **B. 独立 `/chat` 页面** | 空间大，支持表格、代码块、历史会话 | 需要跳转 |

**推荐：先做 B（独立页面），再扩展 A（全局浮动入口）。**

### 6.2 页面元素

- 左侧：会话历史列表 + 新建对话按钮
- 右侧：
  - 消息区域（用户右气泡、AI 左气泡）
  - AI 消息内展示：
    - 思考过程（可折叠）："正在查询 messages 表最近 7 天..."
    - 最终答案
    - 数据源引用："基于 SQL: SELECT ..."（点击展开）
  - 输入框 + 发送按钮 + 快捷问题

### 6.3 交互细节

- 支持 Enter 发送，Shift+Enter 换行。
- AI 回答支持流式输出（SSE）。
- 快捷问题示例：
  - "过去 24h 消息最多的群"
  - "最近有哪些 Key 商线索"
  - "过去 7 天商品价格变化"

---

## 7. 安全与风险控制

| 风险 | 对策 |
|------|------|
| SQL 注入 / 数据破坏 | 强制只读连接；SQL 白名单校验（必须以 SELECT 开头）；禁用 DDL/DML 关键词 |
| 越权访问 | 根据用户角色注入 `allowed_chat_ids`，AI 生成 SQL 时自动附加 `chat_id IN (...)` |
| 数据泄露 | 查询结果限制 100 行；敏感字段脱敏（如 phone 字段） |
| 循环失控 | 设置最大 tool 调用次数（如 10 次）和最大 token 数 |
| 成本爆炸 | 限流；长问题/复杂查询走后台异步任务；记录 token 消耗 |
| 审计 | 记录每次对话、每次 SQL、执行结果到 `chat_messages` 表 |

### 7.1 SQL 安全校验示例

```python
FORBIDDEN_KEYWORDS = {'insert', 'update', 'delete', 'drop', 'alter', 'truncate',
                      'create', 'grant', 'revoke', 'lock', 'exec', 'execute'}

def is_safe_sql(sql: str) -> bool:
    sql_lower = sql.lower().strip()
    if not sql_lower.startswith('select'):
        return False
    return not any(kw in sql_lower for kw in FORBIDDEN_KEYWORDS)
```

---

## 8. 实现路线图

### Phase 1：MVP（1-2 周）

- [ ] 新增 `pydantic-ai` 依赖。
- [ ] 实现 `app/ai_chat.py` + 2 个工具（`run_sql`、`get_schema`）。
- [ ] 暴露 `POST /api/chat` 接口（非流式）。
- [ ] 新增 `/chat` 页面 + 基础消息气泡 UI。
- [ ] 安全：只读连接 + SQL 关键词过滤。

**验收标准**：管理员提问"最近 7 天哪个群消息最多"，AI 能生成正确 SQL 并返回答案。

### Phase 2：增强体验（2-3 周）

- [ ] 流式输出（SSE）。
- [ ] 展示 AI 思考过程和数据源引用。
- [ ] 多轮对话上下文支持。
- [ ] 快捷问题 + 答案复制。

### Phase 3：生产化（3-4 周）

- [ ] 对话历史持久化（`chat_sessions` / `chat_messages`）。
- [ ] 用户权限隔离（普通用户只能查有权限的群）。
- [ ] 审计日志 + token 消耗统计。
- [ ] 预置常用查询模板 / 语义缓存（相同问题直接走缓存）。
- [ ] 可选：升级到 LangGraph 支持更复杂的多步分析流水线。

---

## 9. 依赖变更

在 `requirements.txt` 中新增：

```text
pydantic-ai>=0.0.40
```

> 项目已依赖 `pydantic-settings`，与 `pydantic-ai` 生态一致，兼容性高。

---

## 10. 需要用户确认的问题

1. **使用范围**：是否只对管理员开放，还是普通用户也能用？
2. **数据权限**：普通用户是否需要按群隔离？
3. **模型选择**：DeepSeek 优先使用哪个模型（如 `deepseek-chat` / `deepseek-reasoner`）？
4. **输出形式**：先接受非流式，还是直接上流式？
5. **历史会话**：Phase 1 是否需要保存对话历史？
6. **预算控制**：是否设置每日/每月 AI 调用预算上限？

---

## 11. 总结

| 项目 | 决策 |
|------|------|
| pi.dev | ❌ 不推荐，Node.js 生态，无法直接嵌入 |
| 推荐引擎 | ✅ Pydantic AI（MVP），LangGraph（进阶） |
| 核心能力 | 自然语言 → 表结构理解 → 只读 SQL → 结果解释 |
| 前端形态 | 先做独立 `/chat` 页面，再扩展全局浮动入口 |
| 安全基线 | 只读连接、SQL 白名单、权限隔离、结果限流 |
| 预期工期 | MVP 1-2 周，完整生产化 4-6 周 |

下一步：确认上述问题后，即可进入 Phase 1 实施。
