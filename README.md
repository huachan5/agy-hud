# agy-hud — Antigravity CLI 状态栏 HUD 插件

参考 Claude Code 的 `claude-hud` 设计，为 Antigravity CLI (`agy`) 打造的中文实时状态栏插件（纯 Python 标准库，零外部依赖，已避开 Windows `cmd /c` 路径引号转义坑）。

## 显示内容

```text
[Gemini 4 Neon ◑ high] │ cspace git:(main*) │ ⏱ 12m
上下文 ██░░░░░░ 18% │ AGY余: 85% (3h 13m) | 5h: 30% (3h 13m) | 7d: 98% (1d 10h) | Codex
1 AGENTS.md | 1 MCPs | 21 Skills
✓ Read ×8 | ✓ Shell ×4 | ✓ Edit ×2 | ✓ Write ×1 | +1 more
```

- **第 1 行**：模型名 + 思考强度图标（`○ low / ◔ medium / ◑ high / ◕ xhigh / ● max`）、当前项目与 Git 分支、会话时长。
- **第 2 行**：中文上下文进度条（绿/黄/红阈值）、AGY 本地模型剩余配额（后台异步刷新缓存不阻塞渲染），以及可选的外部 Codex 额度快照（`5h` / `7d`）。
- **第 3 行**：AGY 实际加载的 `AGENTS.md` / `GEMINI.md`、规则、MCP 服务、Skills 与钩子计数。
- **第 4 行**：实时解析当前会话 `transcript.jsonl` 的工具调用统计（运行中 `◐` + 已完成 `✓ 工具 ×次数` + `+N more`）。
- **第 5 行（按需）**：运行中或 60 秒内完成的子代理状态。

## 安装

在 PowerShell 中运行：

```powershell
.\agy-hud\install.ps1
```

安装后重启 `agy` 即可生效。配置文件位于 `~/.antigravity/agy-hud/config.json`，可随时按需开关各模块。
