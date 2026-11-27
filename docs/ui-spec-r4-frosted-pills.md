# agy-hud · R4「冰白微胶囊（`frostedPills`）」UI 设计语言规范

> **设计版本**：R4（`frostedPills`）  
> **适用宿主**：macOS Otty 浅色半透明毛玻璃终端（`#F7F9FA` 宣纸磨砂底 + 1px 输入分隔线）  
> **落地实现**：[hud.py](../hud.py)（`THEME_PRESETS["frostedPills"]` / `_R4_FROSTED_PILLS_STYLE`）  
> **定位**：`agy-hud` 后续需求迭代、新状态接入与新模块扩展的**默认视觉设计规范**。

---

## 1. 设计北极星与环境前提

1. **核心原则**
   > **「微底纹托举焦点、无底纹退后降噪、单点高饱和仅留给危急态」**
2. **渲染边界**
   - 仅使用终端字符单元格可无损绘制的 **24-bit TrueColor 前景色（`38;2`）、浅微底纹（`48;2`）与 Unicode 符号**；不依赖任何浏览器 CSS 阴影、模糊滤镜或 SVG 图形。
   - 默认配合终端字体 `MapleMono-NF-Base.ttf`（Nerd Font）绘制 `\uE0B6` / `\uE0B4` 真圆角微胶囊；同时提供 `style.pillRounded = false` 的纯 ANSI 矩形微底纹零字体回退模式。
3. **配额池与业务前提**
   - **严格双池架构**：固定为 **`Gemini`**（所有 Gemini 模型共用，历史 `argon` 键归并入 Gemini 池）+ **`3P`**（Claude / GPT 等第三方模型），不存在独立 Argon 池。
   - **隐藏冗余套餐行**：`"showPlanTier": false`，不展示 `AI Pro` 等套餐兜底文字。

---

## 2. 信息分层与布局骨架（3 + N 行结构）

```text
● Gemini 4 Hydrogen · medium  claudespace  ⎇ main  CTX ▰▱▱▱▱▱▱ 8% (16k/200k)  <1m
▸ Gemini  5h ━━━━━┄ 88% (2h 8m) │ 7d ━━━┄┄┄ 57% (5d 20h)  ○ 3P  5h 100% · 7d 5%!
✓ Read ×8  ✓ Edit ×2  +1                            ← 动态活动区（按需出现）
● Idle                       1 AGENTS · 3 MCPs · 24 Skills · 1 钩子  ← 状态左对齐，能力贴右对齐
```

| 层级 | 职责与内容 | 视觉与排版规则 |
| :--- | :--- | :--- |
| **第 1 行：执行控制 & 上下文压力** | 模型名、思考档位、托管线程徽标、目录、Git 分支与变更统计、会话标题、`[sandbox]`、`CTX` 进度条、Token 明细、会话时长 | 左侧 `● Model · effort` 置于**淡冰蓝微胶囊** `#EBF3FA`；`●` 随实时状态变色（闲置蓝 / 工作中琥珀 / 异常红）；Git 使用简洁前缀 `⎇ branch`；`CTX` 默认 `contextValue: "both"`，使用 `▰`/`▱` 7 格进度条 + 百分比 + 柔灰 `(used/limit)` Token 明细；元素间以双空格 `  ` 留白分隔 |
| **第 2 行：双池同行微胶囊** | 激活额度池（主焦点）+ 备用额度池（降噪参照） | 激活池独占**清透浅青圆角微胶囊** `#E8F5F3`，内部以 `▸ Pool  5h ━━━━━┄ 88% (2h 8m) │ 7d ━━━┄┄┄ 57% (5d 20h)` 呈现；备用池**禁止铺底纹**，以 `○ Pool  5h 100% · 7d 5%!` 柔灰退后；任一窗口 `≤5%` 或已用 `≥90%` 红标穿透；激活池任一窗口 `≤5%` 时整颗胶囊切换为**温和浅绯红 `#FCEEEF`** 并追加 `↻ 建议换号`；宽屏同行，超出 `terminal_width` 时自动折为两行且不劈断胶囊 |
| **动态活动区（按需）** | 独立 `agy-pool` 账号/低额告警、工具调用、子 Agent 追踪、Todo 进度 | 保持「**动态上移、静态沉底**」：仅在存在活跃或近期内容时渲染，位于第 2 行与最底行之间 |
| **最底行：静态底座 & 实时状态** | 实时状态灯（`● Idle` / `◐ working` / `⏳ waiting` / `⚠ error`）+ `AGENTS / MCPs / Skills / 钩子` | 左对齐无缩进（`envIndent: ""`）；开启 `envRightAlign: true`，右侧能力计数以空格补齐并贴齐第 1/2 行最右边缘；以 ` · ` 分隔；**数字加粗深墨 `#0F172A`，单位柔灰 `#64748B`** |

---

## 3. 完整 Design Token 表（与 `hud.py` `THEME_PRESETS["frostedPills"]` 一致）

### 3.1 胶囊微底纹（Background Tints）

| Token 键 | Hex 色值 | 用途 | 规则 |
| :--- | :--- | :--- | :--- |
| `modelPillBg` | `#EBF3FA` | 第 1 行模型与档位胶囊 | 淡冰蓝，承托顶部焦点锚点 |
| `activePoolBg` | `#E8F5F3` | 第 2 行激活额度池胶囊（正常态） | 清透浅青，呼应 Otty 浅绿透底光晕 |
| `activePoolDangerBg` | `#FCEEEF` | 第 2 行激活额度池胶囊（剩余 `≤5%`） | 温和浅绯红，替代正常浅青并触发 `↻ 建议换号` |

### 3.2 前景墨阶与功能语义色

| Token 键 | Hex 色值 | 用途 |
| :--- | :--- | :--- |
| `model` / `contextValue` / `usage` / 底栏数字 | `#0F172A` | 主墨（加粗）：模型名、健康数值、运行中工具名、底栏能力数字 |
| `label` / `activePoolLabel` / `toolDone` | `#334155` | 次墨：窗口标签 `5h`/`7d`、`CTX` 标签、已完成工具 |
| `modelEffort` / `contextBreakdown` / `envMuted` / `activePoolDim` / `inactivePool` / `quotaBarFillInactive` | `#64748B` | 辅助柔灰：思考档位、CTX Token 明细 `(16k/200k)`、倒计时 `(2h 8m)`、非活跃池文本与健康条、底栏单位 |
| `separator` / `barTrack` / `quotaBarTrackActive` / `quotaBarTrackInactive` | `#CBD5E1` | 发丝结构色：胶囊内 `│`、进度条空轨 `▱`/`┄` |
| `modelPrefix` / `poolBadge` | `#0369A1` | Apple 蓝：待机模型圆点 `●`、托管线程徽标 |
| `project` / `success` | `#047857` | 翡翠绿：项目目录、`● Idle` 状态灯、`✓` 完成标志 |
| `git` / `gitBranch` | `#6D28D9` | 柔紫：Git `⎇` 前缀与分支名 |
| `context` / `quotaBarFill` / `activePoolAccent` | `#0F766E` | 松石青绿：激活池 `▸ Pool`、健康 `CTX` 条、充裕（已用 `<40%`）配额条 |
| `quotaBarFillMid` / `usageWarning` / `warning` | `#B45309` | 琥珀金：过半（已用 `40%–89%`）激活配额条、预警数字 `△`、`◐ working` |
| `custom` | `#C2410C` | 陶土橙：`[sandbox]` 徽标 |
| `critical` | `#DC2626` | 珊瑚红：危急阈值（配额余量 `≤5%` 或已用 `≥90%`、`CTX ≥85%`）、`error` 状态、`↻ 建议换号` |

### 3.3 进度条与阈值双通道微标

- **`CTX` 进度条**：`ctxBarFilled = "▰"`，`ctxBarEmpty = "▱"`，宽度 `barWidth = 7`。`used_pct >= crit (85%)` 后缀 `!`，`>= warn (70%)` 后缀 `△`。
- **配额进度条**：`barFilled = "━"`，`barEmpty = "┄"`，宽度 `quotaBarWidth = 6`；**双通道编码**：`remaining <= 5%` 或 `used >= 90%` 后缀 `!`（珊瑚红），`used >= 75%` 后缀 `△`（琥珀金），确保色觉差异用户也可瞬间识别。

---

## 4. 微胶囊实现协议（`_wrap_frosted_pill`）

1. **连续底色防断洞**：胶囊内部任意子段在拼接时都会输出 `\x1b[0m`（`RESET`）；包裹函数**必须**将内部所有 `RESET` 替换为 `f"{RESET}{bg_ansi}"`，确保整颗胶囊背景色连续不出现白洞。
2. **圆角 vs 矩形双模式**：
   - `pillRounded = true`（默认）：使用背景同色的前景色绘制左右半圆 `{fg_cap_ansi}\uE0B6{RESET}{bg_ansi}{painted}{RESET}{fg_cap_ansi}\uE0B4{RESET}`。
   - `pillRounded = false`：输出纯 ANSI 矩形微底纹 `{bg_ansi} {painted} {RESET}`。
3. **禁止跨行劈裂胶囊**：`render_usage_blocks()` 必须先以 `visual_width(combined) <= max_width` 校验单行宽度；若超出宽度，优先将激活池胶囊与备用池拆为两行，绝不允许在胶囊内部换行。

---

## 5. 后续迭代与新功能接入红线

后续为 `agy-hud` 增加新数据或新功能时，必须按以下规则落位：

| 新增信息类型 | 归属位置 | 视觉约束 |
| :--- | :--- | :--- |
| **执行上下文 / 开关模式**（如新 Mode、托管池标识、异常标记） | 第 1 行 | 若需强调，使用淡彩微底纹胶囊（与 `#EBF3FA` 同明度层级），禁止深色反白块 |
| **实时/高优先度资源监控**（如新配额维度、后台队列） | 第 2 行或独立活动行 | 主焦点进浅青微胶囊，次要参照保持无底纹柔灰 |
| **会话期动态事件**（如新工具类别、子任务、Swarm 状态） | 动态活动区（第 2 行与最底行之间） | 运行中 `◑`/琥珀，已完成 `✓`/翡翠绿，异常 `✗`/珊瑚红 |
| **低频静态环境计数**（如新插件数、Rules 数、Hook 数） | 最底行（与 `AGENTS · MCPs · Skills · 钩子` 并列） | 严格遵循「`{粗体 #0F172A 数字} {柔灰 #64748B 单位}`」，以 ` · ` 连接 |

**绝对红线**：
- 禁止给非活跃池、静态底座或动态日志行铺设背景底色。
- 禁止使用高饱和深色实心背景（会破坏 Otty 浅色毛玻璃的宣纸通透感）。
- 禁止引入第三方 Python 依赖；所有改动必须保持既有的 `default`、`appleVibrancy`、`mintlifyGlass`、`appleSystemBlue`、`quartzTeal` 主题与全量单元测试 100% 通过。
