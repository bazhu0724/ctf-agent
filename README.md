# Magpie + CTF Agent

让 zcode、dsh 或其他具备终端能力的 agent 负责规划与验收，由 Magpie 免费池执行 CTF 单题任务。
这是 [Veria Labs ctf-agent](https://github.com/verialabs/ctf-agent) 的工作流分支；保留 MIT 许可证与上游署名。[上游介绍](docs/UPSTREAM_README.md) 中的比赛成绩属于上游，旧默认值不代表本分支。

## 给 agent 的入口

先读 [AGENTS.md](AGENTS.md)，再读 [接入与交接协议](docs/AGENT_HANDOFF.md)。通过 shell 调用本项目即可，不要求 zcode/dsh 实现 Codex app-server 协议。

```text
外层 agent 规划/复核
  -> ctf-doctor 预检
  -> metadata.yml + distfiles/ 题目目录
  -> 只读分类、2–4 个证据支持的假设
  -> ctf-solve --challenge ... --no-submit
  -> Magpie group/free-workers -> Docker 单题执行
  -> 复核候选与复现脚本
  -> 按任务要求提交，保存 write-up 和交接记录
```

## 首次启动

需要 Python >=3.14、uv、Docker Linux 容器，以及运行中的 Magpie。网关配置见 [MAGPIE_SETUP](docs/MAGPIE_SETUP.md)。所有命令从仓库根目录运行；`.env` 按当前工作目录读取。

```bash
uv sync --locked
# Linux/macOS 用 cp；PowerShell 用 Copy-Item
cp .env.example .env
# 按自己的网关填写 MAGPIE_BASE_URL / MAGPIE_API_KEY
docker build -f sandbox/Dockerfile.sandbox-lite -t ctf-sandbox .
uv run --locked ctf-doctor --json
uv run --locked ctf-doctor --live --json
uv run --locked ctf-solve --challenge examples/hello-ctf --models magpie/group/free-workers --no-submit --no-writeups
```

示例题只验证管线，不代表真实题目的解题能力。`ctf-doctor --live` 只查询模型列表和 Docker 状态，不调用模型推理。
离线预检通过不等于服务可用；模型出现在列表中也不保证当前额度和工具调用都正常。

## 默认行为与资源上限

| 项目 | 本分支行为 |
|---|---|
| Solver | `magpie/group/free-workers` |
| 总控 | 单题不启动内建总控；全赛显式选 `--coordinator claude` 或 `codex` |
| 并发 | 全赛默认 3 个题目槽；独立单题由外层限制为 3 个进程 |
| 输出预算 | `MAGPIE_MAX_TOKENS=8192`，每次响应 |
| 请求预算 | `SOLVER_REQUEST_LIMIT=100`，每个 Pydantic solver run |
| 续跑 | `SOLVER_MAX_BUMPS=2`，初始一轮 + 最多两轮续跑；不是全赛总预算 |
| 兜底 | 不自动更换 provider 或模型，额度失败交回外层 agent |
| 结果 | `--no-submit` 显示未提交候选；无结果 exit 2；异常 exit 非零 |
| 容器 | 启动新任务不清理其他任务的容器；结束释放自己的容器 |
| 费用 | 免费组按 0 估算，其他 Magpie 路由标 unknown |

免费组成员必须在 Magpie 中核对。`$0` 是本地 solver 估算，不是账单证明；内建 Claude/Codex 总控用量不含在该估算内。
明确配置付费模型仍可调用它们。`--race-models` 增加每题 solver 数量，外层须按实际模型数计算并发。

## 真实题目与提交

```bash
uv run --locked ctf-solve --challenge challenges/my-task --models magpie/group/free-workers --no-submit
```

确认候选和复现脚本后，需要提交时在 `.env` 填写对应平台参数，按任务要求去掉 `--no-submit`。
默认 write-up 在 `writeups/`，日志在 `logs/`。无提交模式的 write-up 须按“候选”理解，以平台响应作最终确认。
题目目录、凭据、日志和运行中的 gateway 数据不随代码发布。

## 可选全赛模式

外层 agent 负责规划时用单题模式即可。需要原有全赛调度器时：

```bash
uv run --locked ctf-solve --coordinator codex --max-challenges 3 --models magpie/group/free-workers --no-submit
```

此路径额外依赖已配置的 Codex CLI/账号和比赛平台。本次未做完整线上比赛验收。
同时配置 `magpie/group/auto-beam-free` 时类别偏好才参与两组分流；默认仅用一个免费池。

## 开发验证

```bash
uv sync --locked
uv run --locked pytest -q tests -p no:cacheprovider --import-mode=importlib
uv run --locked ruff check backend tests
uv run --locked ctf-doctor --json
```

CI 在 Linux 和 Windows 执行离线测试与 lint。模型推理、Docker 镜像构建和比赛提交需单独验证。
旧比赛专用 PowerShell 脚本保留作历史参考，新环境请使用通用入口。
