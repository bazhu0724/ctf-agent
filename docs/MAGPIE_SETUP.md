# Magpie 网关配置

本仓库提供 ctf-agent 接入代码和不含账户数据的路由示例。Magpie 程序、供应商插件和登录由各机器自己的 Magpie 安装管理；不复制已有机器的账户文件或整个 data 目录。

1. 启动 Magpie，确认本机 OpenAI-compatible API 地址，常用为 `http://127.0.0.1:3425/v1`。
2. 在 Magpie 中配置并验证 OpenCode Zen 等供应商。免费模型可用性随服务变化，以当前模型列表和供应商额度为准。
3. 创建 `free-workers` 组。字段示例在 [magpie-group.example.json](../config/magpie-group.example.json)：`routing=order`、`affinity=session`；只保留当前可用的免费成员。
4. 示例是单个 group 对象，不是完整 providers.json，也不是保证适配所有版本的导入文件。优先通过 UI 建组，勿直接覆盖现有配置。
5. 本地原组可能包含 WorkBuddy 等按账户积分供应的模型。示例没有纳入这些成员；积分权益与永久免费 API 不等价。
6. 将网关实际调用 key 写入本地 `.env` 的 `MAGPIE_API_KEY`。示例的 `magpie-codex` 只是已有本地接入约定，另一台机器须使用自身配置。
7. 执行 `uv run --locked ctf-doctor --live --json`。`magpie_live: ok` 只证明组 ID 在模型列表中，不审计成员价格。

| 调用方 | Base URL | Model |
|---|---|---|
| 外层 agent 的通用 OpenAI 客户端（可选） | 自己配置的 Magpie `/v1` | `group/free-workers` |
| 本仓库 `ctf-solve --models` | `MAGPIE_BASE_URL` | `magpie/group/free-workers` |

外层 zcode/dsh 可以继续使用自己的规划模型；它们只需能调用 shell，就能使用本仓库。不要把本项目 `magpie/` provider 前缀填进网关的原生 model 字段。

如 agent 在远程机器或容器里运行，`127.0.0.1` 指的是该环境自身。应在同一环境部署网关或使用已配置的可达地址；本仓库不自动开放本机端口。
