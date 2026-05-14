# 使用文档

> 面向：「我要把这个东西跑起来用」。
> 想看怎么实现的，看 [`docs/architecture.md`](architecture.md)。

---

## 0. 它是什么、能干什么

`issue-agent-worker` 是一个**长期运行的代码任务系统**。

```text
飞书 / GitHub Issue
    → Temporal Workflow（durable runtime，挂了能续）
        → LangGraph Agent（planner / coder / tester / reviewer / reporter）
            → Cursor Agent（默认执行器，可换 Claude Code / Codex / OpenHands / Shell）
                → Git worktree / Docker（隔离）
                    → GitHub PR
                        → GitHub Actions CI
                            → CI 失败自动修 / 人工审批
```

**当前阶段（Phase 1）真正做到的事**：

| 能力 | 状态 |
|---|---|
| 配置驱动、零硬编码、跨项目可移植 | ✅ |
| GitHub Issue → Temporal Workflow 调度 | ✅ |
| LangGraph Planner 节点产出计划并写回 Issue 评论 | ✅ |
| 持久化：Temporal history + LangGraph SQLite checkpoint + 磁盘 artifacts | ✅ |
| 稳定 workflow_id / thread_id / artifact key（重启续跑而不是新建） | ✅ |
| Cursor Agent 默认执行器（headless 调用 `cursor-agent`） | ✅ |
| 手动 / Temporal 两种触发方式 | ✅ |
| 写代码 / 跑测试 / 创建 PR / 看 CI / 飞书入口 | 🚧 接口 stub，Phase 2-4 实现 |

---

## 0.5 第一次使用：只需要记住一条命令

```bash
pip install -e .
agent-worker bootstrap
```

完了。它会**一边配置一边测试一边自动修复**，最后告诉你「能用了」或者「卡在第 N 步，原因是 X」。

### `bootstrap` 内部干的事（5 个 phase，按顺序跑）

| # | Phase | 做什么 | 失败会停吗？ |
|---|-------|--------|---|
| 1 | **CONFIG** | 没有 YAML 就启动向导问你 4 个问题；有就直接用 | ✓ |
| 2 | **PREFLIGHT** | 13 项体检 —— gh 登录、仓库可访问、push 权限、12 个 label、本地 checkout、cursor-agent 可用、目录可写 ……。能自动修的（建 label、clone 仓库、`mkdir -p`）就自动修 | ✓ |
| 3 | **SMOKE** | 在「假 issue」上跑一轮 planner，证明 LangGraph 主循环能走通（不联网） | ✓ |
| 4 | **LIVE_READ** | 拉一个**真实的 GitHub issue** 跑一轮 planner（不发评论，不开 PR），证明能在你真的代码上思考 | ✓ |
| 5 | **FULL** | （需要 `--full` 才跑）`docker compose up -d` + 起 Temporal worker + 派发真任务 + 开真 PR | — |

### 真实输出（在 dimos 仓库跑出来的）

```text
✓ CONFIG: Using configs/example-dimos.yaml                    (0.0s)
✓ PREFLIGHT: all 13 checks passed                             (3.5s)
✓ SMOKE: plan.md written (682 bytes)                          (0.2s)
✓ LIVE_READ: planned for #31 (4255 bytes)                    (21.4s)
· FULL: opt-in only

✓ Bootstrap complete.
```

任何一步 FAIL，输出会直接告诉你**哪个 phase 挂了 + 具体错误 + 下一步该干啥**。修了重新跑 `agent-worker bootstrap` 即可，**不需要记别的命令**。

### 常用变体

```bash
agent-worker bootstrap                       # 交互式，会问你确认
agent-worker bootstrap --yes                 # 全部默认值，不问 —— CI 用
agent-worker bootstrap --test-issue 42       # 用 issue #42 做 LIVE_READ
agent-worker bootstrap --full                # 一路跑到真的开 PR
agent-worker bootstrap --full --yes          # 一条龙到底
```

---

## 1. 安装

### 1.1 系统要求

- Python ≥ 3.11（推荐 3.12，本地用 `uv` 装）
- `gh` CLI 已登录（`gh auth status` 不报错）
- 想跑完整 Temporal 流：Docker
- 想用 Cursor 当执行器：`cursor-agent` 在 PATH 上（或用 `CURSOR_AGENT_BIN` 环境变量指定路径）

### 1.2 装环境

```bash
cd /home/lenovo/issue-agent-worker
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"      # 含开发依赖（pytest / ruff / mypy）

# 可选：飞书 webhook 服务器
uv pip install -e ".[feishu]"
```

跑测试确认环境对：

```bash
pytest -q
# 应该看到：25 passed
```

### 1.3 配置环境变量

```bash
cp .env.example .env
$EDITOR .env
```

至少要设：

```bash
AGENT_WORKER_CONFIG=configs/my-project.yaml   # 待会创建
GITHUB_TOKEN=                                  # 留空也行，会用 gh auth login 的凭证
```

可选：

```bash
TEMPORAL_HOST=localhost:7233
CURSOR_AGENT_BIN=/usr/local/bin/cursor-agent   # 如果 cursor-agent 不在 PATH
```

---

## 2. 给你的项目写配置（这是可移植性的全部）

**关键点：所有跟项目相关的东西都在 YAML 里，代码里没有任何硬编码。**

> 推荐用 `agent-worker init` 交互生成；下面的手写步骤只在你想完全控制时用。

### 2.1 交互式生成（推荐）

```bash
agent-worker init                # 写 configs/<your-project>.yaml + 更新 .env
agent-worker doctor              # 看哪些没配好
agent-worker doctor --fix        # 自动建 label / clone / mkdir
```

或者一条命令搞定：

```bash
agent-worker bootstrap           # = init + doctor --fix
```

### 2.1b 手写配置文件

```bash
cp configs/example-dimos.yaml configs/my-project.yaml
$EDITOR configs/my-project.yaml
```

### 2.2 必填字段

```yaml
project:
  mode: existing                       # existing = 优化已有项目；scaffold = 从 0 开始
  description: "一句话说明 worker 干什么的"  # 会被注入 planner prompt

repo:
  owner: <你的 GitHub 账户或组织>     # e.g. "myorg"
  name: <仓库名>                      # e.g. "my-service"
  base_branch: main                    # 你的默认分支
  local_path: /absolute/path/to/repo   # 本地 checkout 路径，Phase 2 worktree 会用

commands:
  setup:
    - "<装依赖的命令>"                # e.g. "uv sync" / "npm install"
  lint:
    - "<lint 命令>"                   # e.g. "ruff check"
  test:
    - "<测试命令>"                    # e.g. "pytest"
  build: []
```

### 2.3 选执行器

```yaml
executor:
  default: cursor                       # 改这一行就换执行器
  cursor:
    enabled: true
    command: cursor-agent
    args_template:
      - "--print"
      - "{prompt}"
    timeout_seconds: 1800
```

如果你的 `cursor-agent` 用法不一样（比如要 `--workspace` 或 `--model`），改 `args_template`，里面支持 `{prompt}` `{workspace}` `{model}` 三个占位符。

要换成 Claude Code：

```yaml
executor:
  default: claude_code
  claude_code:
    enabled: true
    command: claude
    args_template:
      - "-p"
      - "{prompt}"
```

### 2.4 验证配置

```bash
agent-worker show-config
agent-worker list-executors
```

`show-config` 会把所有层级合并后的最终配置打印成表格，能看出：
- 哪些值来自 default.yaml
- 哪些被你的 YAML 覆盖了
- 哪些被环境变量覆盖了

---

## 3. 三种运行方式

按从轻到重排：

### 3.1 干跑（dry-run）—— 不调外部 API，验证管道

```bash
agent-worker run-once --issue 1 --repo acme/widget --dry-run
```

会做什么：
- 用一个假的 issue body
- 跑完整的 LangGraph 一轮（load_context → planner → reporter）
- 在 `runs/acme--widget--issue-1/` 写出 plan.md / todo.md / handoff.md
- 不调 `gh`，不调 Temporal

**用途**：验证 executor 配置对不对、artifact 目录布局正常、planner prompt 能用。

### 3.2 单轮运行（run-once，不要 Temporal）

```bash
agent-worker run-once --issue 42
```

会做什么：
- `gh issue view` 拉 Issue 内容
- 跑 LangGraph 一轮
- 把 reporter 生成的评论**返回到终端**（不发回 Issue，避免噪音）
- 写 artifacts 到磁盘

**用途**：日常本地调试 prompt、验证某个 issue 的 plan 质量。

### 3.3 完整流程（Temporal + Worker）

```bash
docker compose up -d                              # 起本地 Temporal
agent-worker worker &                             # 启动 worker 进程
agent-worker run-issue --issue 42 --wait          # 派发 workflow
```

会做什么：
- Worker 监听 task queue `issue-agent-worker`
- 客户端创建一个 workflow，id = `issue-agent--<owner>--<repo>--issue-42`
- Workflow 跑完整序列：
  1. `transition_issue_label`（agent:todo → agent:running）
  2. `load_issue`（gh issue view）
  3. `transition_issue_label`（→ agent:planning）
  4. `run_agent_round`（执行 LangGraph）
  5. `post_issue_comment`（gh issue comment，**真的发出去**）
  6. `transition_issue_label`（→ agent:blocked，等下一阶段）

**用途**：生产用法。每一步都受 Temporal 管控，挂了能恢复。

打开 Temporal UI 看 workflow 历史：<http://localhost:8233>

### 3.4 自动派发（不需要再手动 `run-issue`）

把 worker 跑起来之后，可以让它**自己扫 GitHub**，看到 `agent:todo` 自动派发。三种方式：

```bash
# A. 最简单：worker 内嵌 dispatcher 跑一个进程
agent-worker worker --with-dispatcher

# B. 单独跑（多 worker 场景，只让一个进程负责派发）
agent-worker worker            # 终端 1
agent-worker dispatcher        # 终端 2

# C. 永远在：docker-compose 起整套
docker compose up -d           # docker-compose.yml 里的 worker 服务 restart=always
```

要让 `worker` 默认带 dispatcher（不用 `--with-dispatcher` 参数），在项目 YAML 里：

```yaml
workflow:
  dispatcher:
    enabled: true
    poll_interval_seconds: 30
    max_dispatch_per_cycle: 10
    auto_recover_blocked: true
    blocked_recover_min_interval_seconds: 600
```

每一轮 dispatcher 干的事：

1. **接新任务**：`gh issue list --label agent:todo` → 每个 issue 调 `start_issue_workflow`。idempotent —— workflow id 稳定，已经在跑的会自动 attach，已经结束的会自动 restart 重跑。
2. **复活 blocked**：扫 `agent:blocked`，找对应 PR，poll CI（用 `github.ci_ignore_workflows` 把人工审批类的 workflow 过滤掉）。
   - CI 真过了 → 打 `agent:done` + 评论
   - CI 真挂了 → 再派发一次 workflow（coder 会从 prior_failure 重试）
   - CI 还在跑 → 不动，下一轮再看
   每个 issue 的复活有节流（默认 10 分钟一次），避免 CI 抖动导致死循环。

一次性调试用：

```bash
agent-worker dispatcher --once     # 跑一轮立刻退出，打 JSON 摘要
```

输出示例：

```json
{
  "todo_seen": 2,
  "todo_dispatched": 2,
  "todo_attached_running": 0,
  "todo_restarted": 0,
  "blocked_seen": 1,
  "blocked_marked_done": 0,
  "blocked_redispatched": 0,
  "blocked_skipped_pending": 1,
  "blocked_skipped_throttled": 0,
  "errors": 0
}
```

### 3.5 过滤"假 CI"workflow

有的仓库 CI 里会挂一些"非代码门禁"的 workflow —— 比如 dimos 的 `Auto Merge` 会轮询 Codex bot 给 👍。这种东西，agent 等不到也修不了。

在项目 YAML 里告诉 worker 哪些 workflow 名字直接忽略（大小写不敏感的子串匹配）：

```yaml
github:
  ci_ignore_workflows:
    - "Auto Merge"        # dimos 的人工审批环节
    - "Codex Review"
```

被忽略的 workflow **不计入** CI 通过/失败判定。如果一个 PR 上所有 workflow 都被忽略了，CI 状态记为 `unknown`，dispatcher 会继续等（不会贸然标 done）。

---

## 4. CLI 命令对照表

| 命令 | 用途 | 联网吗 | 需要 Temporal 吗 |
|---|---|---|---|
| `agent-worker bootstrap [--full] [--yes] [--test-issue N]` | **唯一你需要记的命令**：5 phase 一条龙，配置 + 预检 + 烟雾测试 + 真 issue 测试 + （可选）启动 worker 开真 PR | gh ✅ | `--full` 才需 |
| `agent-worker init` | 单跑「交互向导」（被 bootstrap 自动调） | 部分（gh 自动检测） | 否 |
| `agent-worker doctor [--fix] [--strict]` | 单跑「预检体检」（被 bootstrap 自动调） | gh ✅ | 否 |
| `agent-worker start` | 一键启动 worker（不跑 bootstrap 的测试 phase） | gh + docker ✅ | ✅ |
| `agent-worker show-config` | 看合并后的配置 | 否 | 否 |
| `agent-worker list-executors` | 列出已注册执行器和默认 | 否 | 否 |
| `agent-worker artifact-path --issue N --repo o/r` | 打印某 issue 的磁盘目录 | 否 | 否 |
| `agent-worker run-once --issue N --dry-run` | 假数据跑一轮 | 否 | 否 |
| `agent-worker run-once --issue N` | 真 issue 跑一轮，结果只在终端 | gh ✅ | 否 |
| `agent-worker run-issue --issue N` | 派发到 Temporal（手动派发单个 issue） | gh + temporal ✅ | ✅ |
| `agent-worker worker [--with-dispatcher]` | 启动 Temporal worker。`--with-dispatcher` 内嵌自动派发器 | temporal ✅ | ✅ |
| `agent-worker dispatcher [--once] [--interval N]` | 单独跑「自动派发器」：扫 `agent:todo` 自动派发、复活 `agent:blocked`（见 §3.4） | gh + temporal ✅ | ✅ |
| `agent-worker feishu-server` | 起飞书 webhook（Phase 4 stub） | 否 | 否 |

通用 flag：

- `-c / --config <path>`：用别的 YAML 覆盖 `AGENT_WORKER_CONFIG`
- `--log-level DEBUG / INFO / WARNING`

---

## 5. 配置覆盖优先级（重要）

> 这是「可移植性」实际工作的方式。

```text
低 ┌────────────────────────────────────────┐ 高
   │ 1. configs/default.yaml                │
   │ 2. 项目 YAML（AGENT_WORKER_CONFIG / -c）│
   │ 3. .env 文件                            │
   │ 4. 环境变量                             │
   │ 5. CLI flag                             │
   └────────────────────────────────────────┘
```

**深度合并**：dict 递归合并；list 整个替换（不拼接）。

### 环境变量两套写法

**短名（常用键，方便记）**：

```bash
TEMPORAL_HOST=temporal.prod:7233
TEMPORAL_TASK_QUEUE=my-queue
ARTIFACT_ROOT=/var/agent/runs
LANGGRAPH_CHECKPOINT_DB=/var/agent/lg.sqlite
CURSOR_AGENT_BIN=/usr/local/bin/cursor-agent
AGENT_WORKER_CONFIG=configs/my-project.yaml
```

**通用模式（任意嵌套字段）**：`AGENT_WORKER__SECTION__FIELD`

```bash
AGENT_WORKER__REPO__OWNER=acme
AGENT_WORKER__REPO__BASE_BRANCH=develop
AGENT_WORKER__EXECUTOR__DEFAULT=claude_code
AGENT_WORKER__WORKFLOW__MAX_RETRIES=3
AGENT_WORKER__COMMANDS__TEST='["pytest","ruff check"]'   # list 用 JSON
```

值会自动判类型：`true/false` → bool，纯数字 → int/float，`[`/`{` 开头 → JSON，否则字符串。

### 实战：临时换执行器跑一次

```bash
AGENT_WORKER__EXECUTOR__DEFAULT=stub agent-worker run-once --issue 1 --dry-run
```

不动配置文件就完成了切换。

---

## 6. 看 artifacts（agent 的"工作记录"）

每个 issue 一个目录：

```text
runs/<owner>--<repo>--issue-<n>/
├── input/
│   └── issue.md              ← 原始 Issue 内容
├── planning/
│   ├── plan.md               ← Planner 产出的完整计划
│   ├── todo.md               ← 提取的 subtasks
│   └── assumptions.md        ← Planner 写的假设
├── execution/
│   ├── commands.log          ← Phase 2 起：跑过的命令
│   ├── tool_calls.jsonl      ← 每次 executor 调用一行
│   └── changed_files.txt     ← Phase 2 起：本轮改动的文件
├── evidence/
│   ├── local_tests.log       ← Phase 2 起：本地测试输出
│   └── ci_logs.md            ← Phase 4 起：CI 失败日志摘要
├── review/
│   ├── self_review.md        ← Phase 2 起：自审报告
│   └── risk_report.md
└── handoff.md                ← 下一轮 / 下次重启的接手说明
```

**为什么搞这么多文件？** 长期 agent 跑几小时几天后会"失忆"。artifacts 是它的外置记忆，下次重启读 `handoff.md` 就能接着干。

快速看：

```bash
agent-worker artifact-path --issue 42 --repo acme/widget
# /home/lenovo/issue-agent-worker/runs/acme--widget--issue-42

cat $(agent-worker artifact-path --issue 42 --repo acme/widget)/handoff.md
```

---

## 7. GitHub Issue 状态流转（label 驱动）

Workflow 用 label 表示 Issue 状态：

```text
agent:todo
   ↓ Dispatcher / 手动派发
agent:running          ← workflow 启动
   ↓
agent:planning         ← LangGraph planner 跑
   ↓ Phase 1 在这里停
agent:blocked          ← 等 Phase 2 启用
   ↓ Phase 2-4
agent:coding → agent:testing → agent:pr-created → agent:ci-running → agent:review → agent:done
```

label 名字全在配置里，按公司风格改：

```yaml
github:
  issue_label_todo: "ai:todo"        # 改前缀
  issue_label_running: "ai:running"
  ...
```

---

## 8. 常见问题

### Q1. `gh: command not found`

装 GitHub CLI（<https://cli.github.com>），登录：

```bash
gh auth login
gh auth status        # 应该看到 "Logged in to github.com"
```

代码里调 `gh` 是为了**可移植性** —— 用户已经登录的凭证立刻能用，不用重新做 token 管理。

### Q2. Temporal 起不来

```bash
docker compose ps        # 看 temporal / temporal-ui 是不是 healthy
docker compose logs temporal | tail -50
```

UI 在 <http://localhost:8233>。如果端口冲突，改 `docker-compose.yml`。

### Q3. Workflow 重复 / 想清掉重跑

```bash
# 用 Temporal CLI 取消
docker exec agent-worker-temporal tctl workflow cancel \
    --workflow_id "issue-agent--acme--widget--issue-42"

# 或者改 workflow_id 后缀强制新跑（不推荐，会失去续跑能力）
```

`run-issue` 默认 `reuse_existing=True`，对同一个 issue 重复执行会 attach 到现有的 workflow，**不会重复创建**。这就是稳定 ID 的好处。

### Q4. LangGraph checkpoint 想重置

```bash
rm -f checkpoints/langgraph.sqlite
```

或者改配置：

```yaml
langgraph:
  checkpoint_backend: memory   # 不持久化，每次干净
```

### Q5. Cursor agent 调用超时

```yaml
executor:
  cursor:
    timeout_seconds: 3600   # 默认 1800
```

或者临时：

```bash
AGENT_WORKER__EXECUTOR__CURSOR__TIMEOUT_SECONDS=3600 agent-worker run-issue --issue 1
```

### Q6. 想看具体跑了什么命令

```bash
# 终端开 DEBUG
agent-worker --log-level DEBUG run-once --issue 1

# JSON 日志（生产用）
AGENT_WORKER__SYSTEM__LOG_FORMAT=json agent-worker worker

# Temporal 历史里能看到每个 activity 的入参和返回
# 在 UI 里点 workflow → "History" tab
```

### Q7. 怎么扩展（加新 executor / 新节点）

看 [`docs/architecture.md`](architecture.md) 第 8 节「扩展点」。要点：

- 新 executor：在 `app/executors/` 加文件，调 `register_executor("name", Cls)`
- 新节点：在 `app/langgraph_app/nodes/` 加文件，在 `graph.py` 里加边
- 新 activity：在 `app/temporal_app/activities.py` 加方法 + 在 workflow 里 `execute_activity`

---

## 9. 一些有用的小命令

```bash
# 看哪些执行器装了
agent-worker list-executors

# 看 artifact 目录在哪
agent-worker artifact-path --issue 42 --repo acme/widget

# 把当前合并后的配置 dump 成 JSON（脚本用）
agent-worker show-config --json | jq .repo

# 跑一个 ruff 检查
ruff check app/

# 跑测试
pytest -q

# 跑指定测试
pytest tests/test_config.py -v

# 看可移植性测试有没有抓到硬编码
pytest tests/test_config.py::test_no_hardcoded_repo_or_branch_in_app_code -v
```

---

## 10. 上生产前的 checklist

- [ ] `configs/<project>.yaml` 用真实 repo / commands 填好
- [ ] `.env` 设了 `AGENT_WORKER_CONFIG` 和必要的 token
- [ ] `gh auth status` 通过
- [ ] `agent-worker show-config` 输出符合预期
- [ ] `agent-worker run-once --issue <真实 issue 号> --dry-run` 跑通
- [ ] Temporal 起来：`docker compose up -d`
- [ ] Worker 起来：`agent-worker worker` 看到 `worker.starting` 日志
- [ ] `agent-worker run-issue --issue <真实 issue 号> --wait` 看到 `final_status: planning_done`
- [ ] Issue 里看到 bot 评论
- [ ] `runs/<key>/handoff.md` 内容正确
- [ ] 杀掉 worker 重启，看 Temporal UI 能恢复 workflow

全过即可上 Phase 2 实施。
