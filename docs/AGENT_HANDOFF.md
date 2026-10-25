# 外层 Agent 接入与交接

本文件给 zcode、dsh 或任何能执行 shell 的外层 agent。接入方式是普通命令行调用，不要求、也不承诺原生 MCP 或 app-server 适配。外层 agent 负责选题、假设、复核与最终验收；`ctf-solve` 只负责单题执行。

## 可复制任务提示词

```text
你是外层规划 agent。工作目录为 ctf-agent 仓库根目录，只通过 shell 调用 ctf-solve。

1. 题目准备：确认题目目录含 metadata.yml 与 distfiles/；不要修改只读附件。
2. 只读分类：只读 metadata.yml、附件清单和文件类型，判断类别与可用线索。
3. 假设：写出 2-4 个有证据支持的假设，按验证成本排序，先做最便宜的判别实验。
4. 执行：最多同时运行 3 个 worker，各自使用独立题目目录，互不写入；单题使用
   magpie/group/free-workers，不加 --coordinator，不做递归委派。
5. 证据复核：核对命令、输入、字面输出、退出码和复现脚本，区分候选与平台确认。
6. 提交/复盘：需要提交时由外层决定并去掉 --no-submit；保存 write-up 与交接记录。

回报必须包含实际执行的命令、cwd、输入、字面输出、退出码、产物路径、
已验证/已否证的假设和剩余不确定性。未运行的测试不得声称通过。
```

## 通用 CLI

在仓库根目录执行，先预检再解题：

```bash
uv run --locked ctf-doctor --json
uv run --locked ctf-doctor --live --json
uv run --locked ctf-solve --challenge challenges/<题目> --models magpie/group/free-workers --no-submit
```

`ctf-doctor` 默认离线；`--live` 只查询 Magpie 模型列表与 Docker 状态，不消耗推理。`--live` 需要 `MAGPIE_BASE_URL`（常用 `http://127.0.0.1:3425/v1`）可达。

单题模式不会启动内建总控。全赛模式必须显式指定 `--coordinator claude` 或 `--coordinator codex`；两者之外没有已验证的 coordinator 后端，外层 agent 应直接用单题命令。默认并发 3，需要更少时显式传 `--max-challenges`。

`--no-submit` 输出的 `FLAG CANDIDATE (not submitted)` 只是未提交候选，不是平台确认。无结果时退出码为 2；参数错误同样为 2；其他异常为非零。

模型名有两种写法：本仓库 `--models` 使用 `magpie/group/free-workers`；直接调用网关的通用 OpenAI 兼容客户端时，model 字段填 `group/free-workers`，base URL 用 `http://127.0.0.1:3425/v1`。这是通用兼容设置，不涉及任何产品专用配置路径。

## 证据模板

```text
题目：<name> (<category>)
假设：H1 <描述> | 证据：<文件/输出> | 状态：验证/否证/未测
命令：<完整命令>
cwd：<绝对路径>
输入：<题目目录、附件、服务地址>
字面输出：<关键行，原样粘贴>
退出码：<0/1/2/...>
产物：<write-up、脚本、日志的绝对路径>
结论：候选 <flag> / 无结果
剩余不确定性：<未覆盖的分支、未验证的环境>
```

## 并发与资源

- 同时最多 3 个 worker，各自独立题目目录；禁止子 worker 再委派。
- 每题只用 `magpie/group/free-workers`，不自动切换付费模型或 provider。
- 免费池成员须在 Magpie 中核对；`$0` 是本地估算，不是账单。
- 不清理其他任务的容器，不批量删除文件，不使用递归删除命令。

## 失败与恢复

1. 预检失败：先修 Python 3.14+、Docker 或 Magpie 可达性；离线通过不代表服务可用。
2. 模型缺失：`ctf-doctor --live` 报 `magpie_live: fail` 时，核对组 ID 与网关模型列表，不要改写成其他 provider。
3. 退出码 2：先看是否无 flag 或参数错误；保留原始输出，不重复完全相同的实验。
4. 中断恢复：用同一题目目录重跑单题命令；先读 `logs/` 中最近的 trace，确认上次进度，再补做未验证假设。
5. 预算耗尽：停止该题并回报已否证的假设；由外层决定是否换题或调整预算，不在内部静默重试。
