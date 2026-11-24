# agy-hud — Antigravity CLI 状态栏 HUD 插件

参考 Claude Code 的 `claude-hud` 设计，为 Antigravity CLI (`agy`) 打造的中文实时状态栏插件（纯 Python 标准库，零外部依赖，已避开 Windows `cmd /c` 路径引号转义坑）。

## 显示内容

```text
[Gemini 4 Neon ◑ high] │ cspace git:(main*) │ ⏱ 12m
上下文 ██░░░░░░ 18% │ Gemini 5h: 92% (4h 12m) | 7d: 88% (5d 3h) │ 3P 5h: 75% (2h 40m) | 7d: 81% (6d 1h)
1 AGENTS.md | 1 MCPs | 21 Skills
✓ Read ×8 | ✓ Shell ×4 | ✓ Edit ×2 | ✓ Write ×1 | +1 more
```

- **第 1 行**：模型名 + 思考强度图标（`○ low / ◔ medium / ◑ high / ◕ xhigh / ● max`）、当前项目与 Git 分支、会话时长。
- **第 2 行**：中文上下文进度条（绿/黄/红阈值）+ **AGY 原生双模型配额池**：
  - 直接读取 `agy` 传入的 `stdin.quota`，同时支持 **Gemini 池**（`gemini-5h` / `gemini-weekly`）与 **第三方 3P 池**（`3p-5h` / `3p-weekly`，如 Claude/GPT 系列），并自动把当前模型所属配额池排在前面。
  - 可在 `config.json` 中切换 `quotaPools`（`"both"` 双池同显 / `"active"` 仅显示当前模型所在池）和 `usageValue`（`"remaining"` 显示剩余% / `"used"` 显示已用%）。
- **第 3 行**：AGY 实际加载的 `AGENTS.md` / `GEMINI.md`、规则、MCP 服务、Skills 与钩子计数。
- **第 4 行**：实时解析当前会话 `transcript.jsonl` 的工具调用统计（运行中 `◐` + 已完成 `✓ 工具 ×次数` + `+N more`）。
- **第 5 行（按需）**：运行中或 60 秒内完成的子代理状态。

## 安装

在 PowerShell 中运行：

```powershell
.\agy-hud\install.ps1
```

安装后重启 `agy` 即可生效。配置文件位于 `~/.antigravity/agy-hud/config.json`，可随时按需开关各模块。
