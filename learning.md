# Learning Notes

## 项目概览

一个 8 阶段 LLM 驱动的漏洞挖掘流水线，总约 2,500 行 Python 代码。audit 本身是纯调度器，不含任何漏洞检测逻辑 — 所有智能都在 LLM Agent（通过 OpenCode Server API 调用 LLM）中完成。

### 三层架构

```
audit (Python 调度器)        — 阶段编排、循环控制、预算检查、SQLite 持久化
    │ 每个阶段调用 run_agent()
    ▼
OpenCode Server API            — HTTP 调用 opencode serve REST API, 传入 prompt + schema
    │
    ▼
LLM (通过 OpenCode 配置)      — Agent 自主使用 Read/Grep/Glob/Bash 探索仓库，按 prompt 方法论执行
```

## 8 阶段流水线

```
Recon → Hunt → Validate → Gapfill → Dedupe → Trace → Feedback → Report
         ↑                         │         ↑                        │
         └─── Gapfill 循环 ────────┘         └─── Feedback 循环 ──────┘
```

### 两层循环

- **外层（Gapfill 循环）**: Recon → Hunt → Validate → Gapfill → 回到 Hunt。Gapfill 分析覆盖率，为未覆盖的 subsystem × attack_class 组合生成新任务
- **内层（Feedback 循环）**: Dedupe → Trace → Feedback → Hunt → Validate → Dedupe → Trace → 回到 Feedback。Feedback 根据可触达的漏洞 trace 生成深入攻击任务

### 阶段职责

| 阶段 | 模型 | 工具 | 职责 |
|---|---|---|---|
| Recon | deepseek-v4-flash | Read, Grep, Glob, Bash | 扫描仓库，划分子系统，识别入口点和信任边界，挖 git 历史，生成初始任务 |
| Hunt | deepseek-v4-flash | Read, Grep, Glob, Bash | 单个 attack_class + target_files，从 sink 追踪到 source，编写 PoC |
| Validate | deepseek-v4-pro | Read, Grep, Glob | 对抗性代码审查（无 Bash），假设 Hunter 是错的，寻找良性解释 |
| Gapfill | deepseek-v4-pro | Read, Grep, Glob | 构建 subsystem × attack_class 矩阵，找未覆盖的组合生成新任务 |
| Dedupe | deepseek-v4-pro | Read | 去重 confirmed findings，标记 canonical |
| Trace | deepseek-v4-pro | Read, Grep, Glob, Bash | 从 finding 的 sink 反向追踪到 entry point，确认是否可达 |
| Feedback | deepseek-v4-flash | Read, Grep, Glob | 分析可触达 trace，生成深入攻击任务 |
| Report | deepseek-v4-flash | Read | 汇总所有 confirmed + canonical + reachable 的 findings |

## 阶段间通信机制

全部通过 **SQLite 数据库** 作为中介，非内存传对象：

| 表 | 写入者 | 读取者 |
|---|---|---|
| recon_outputs | Recon | Hunt, Gapfill, Trace, Feedback |
| tasks | Recon, Gapfill, Feedback | Hunt（读 pending） |
| findings | Hunt | Validate, Gapfill, Dedupe, Trace, Report |
| traces | Trace | Feedback, Report |
| dedupe_groups | Dedupe | Report |
| costs / artifacts | 所有阶段 | orchestrator（预算检查） |

**Finding 状态机**: NULL（未验证）→ confirmed/rejected（Validate）→ is_canonical（Dedupe）→ reachable（Trace）。只有通过全部筛选才进入 Report。

## Recon 阶段

### 7 步扫描方法

1. **顶层扫描**: `ls -la`、README、构建文件，识别主语言
2. **子系统分解**: 识别 3-15 个功能内聚的子系统，按逻辑而非目录划分
3. **入口点发现**: HTTP 路由、CLI 参数、消息处理器、env-var 消费者
4. **信任边界绘制**: 数据从低信任区到高信任区的跨界点
5. **外部输入枚举**: 具体输入名称 + 控制者角色
6. **Git 历史挖掘**: 搜索 CVE/安全补丁，识别修复模式，grep 同类未修复代码
7. **任务队列**: 生成 30 到 max_tasks 个任务，每个精确到一个 attack_class + 一个子系统

### 子系统划分

完全由 Recon Agent（LLM）自主完成，代码中无划分逻辑。原则：按功能而非目录（同一目录可能有多个子系统）。

## Hunt 阶段

### 漏洞发现机制

**不是逐文件扫描**，而是任务驱动：
- 每个任务 = 1 个 attack_class + 1 个子系统 + 具体 target_files
- Agent 从 sink 反向追踪到 source，只有不可信输入可达才记为 finding
- 并发 50 个 Agent 同时执行

### 核心概念：Source 和 Sink

- **Source（源）**: 不可信数据进入系统的位置（HTTP 请求体、URL 参数等）
- **Sink（汇）**: 数据被危险使用的位置（拼 SQL、`os.system()`、反序列化等）
- 审计核心工作 = 追踪 source → sink，判断是否有清洗逻辑

### PoC 策略

- **有 live_target**: 端到端 PoC（curl/Python 打真实服务）
- **无 live_target**: 提取漏洞逻辑到 scratch 目录，编译运行独立片段
- 两种互补：live_target 验证"真实环境打得通"，代码片段验证"这段逻辑本身有 bug"

## 覆盖率机制

1. **构建覆盖矩阵**: subsystem × attack_class
2. **标记已完成**: 将 completed_tasks 填入矩阵
3. **选择候选项**: 优先 gaps_observed 中的区域 + 无 finding 的子系统 + 确实可能适用的攻击类型
4. **避免重复**: 不重新生成 (subsystem, attack_class) 已覆盖的组合
5. **覆盖分析完全由 LLM 完成**，代码只负责收集数据

## 任务来源

| 来源 | 生成方式 | 特点 |
|---|---|---|
| Recon（初始） | 启发式，偏向高价值组合 | 分布不均匀，auth_bypass 占了 12/48 |
| Gapfill | 严格按 subsystem × attack_class 矩阵查漏补缺 | t_gf_ 前缀 |
| Feedback | 根据已确认的可触达漏洞 trace 自由发散 | t_fb_ 前缀，不受矩阵约束 |

Finding ID 命名规则: `f_<task_id_short>_<n>`，可通过 ID 反推来源任务。

## 错误处理

### 瞬态 API 错误

runner.py 中的 `_classify_api_error()` 根据错误消息文本分类：
- 配额耗尽（QuotaExhaustedError）→ 立即中止
- 已知瞬态错误（overloaded/503/502/504/500/connection errors）→ 重试
- 未知错误 → 兜底为重试（宁可重试也不遗漏）

错误标记列表已适配 OpenCode API 的模式。

## 已知问题与改进方向

### Agent 自主启动目标服务

在没有 `--live-target` 的情况下，多个 Hunt Agent 通过 `mvn spring-boot:run` 自主启动了目标 Spring Boot 应用来验证 PoC。这导致：
- 多个 Agent 同时启动导致端口冲突
- Agent 之间互相 `pkill -f "spring-boot:run"` 杀进程
- 不可预测的端口使用（8080、8888、9999）

**改进方向**:
1. Prompt 层面：明确禁止 Agent 启动目标项目
2. 工具层面：无 live_target 时限制 Bash 访问 localhost
3. 编排层面：orchestrator 统一检测 build commands 并启动一次，注入 live_target

### Validate 的工具限制

Validate 阶段配置 `tools: [Read, Grep, Glob]`（无 Bash），仅做代码审查。但 prompt 提到"如果有 live_target 可以用 Bash 打真实服务"与实际工具配置不一致。

### Dedupe 效果

Report 中存在重复 finding（如两个转账 Oracle 发现，f_t_fb_021_1 和 f_fb006_1），Dedupe 阶段未完全去重。
