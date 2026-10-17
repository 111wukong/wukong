# Agent UX 竞品调研：把"世界最优秀"拆成可落地的设计决策

> 调研时间：2026-10-17。以官方文档与仓库 README 为主，辅以少量第三方分析。
> 标注：**【事实】**＝我在链接页面读到原文；**【推断】**＝基于原文 + wukong 代码的工程判断。
> wukong 基线：读了 `src/wukong/api/console/{index.html,app.js,style.css}`（161/1579/1402 行）、`api/agui.py`、`api/app.py`。903 个测试覆盖内核，不覆盖 UI。

---

## 1. Claude Code：同一份会话，多个"镜头"

**UI**【事实，[interactive-mode](https://code.claude.com/docs/en/interactive-mode)、[sub-agents](https://code.claude.com/docs/en/sub-agents)、[statusline](https://code.claude.com/docs/en/statusline)】

单列 TUI：上方 transcript 向上滚，底部常驻输入框，输入框下方是**子代理面板**（树形，带 `(+N)` 计数），最底一行 statusline。关键不是布局，是同一份会话有多个视图：

- `Ctrl+O` 打开 transcript viewer 看工具细节、时间戳、模型；`[` 把对话写进终端原生 scrollback 以便 `Cmd+F`。
- `Ctrl+T` 待办清单，**一次最多 5 项**，三态；空列表默认折叠；`--resume` 恢复展开状态。
- `/diff` 在 fullscreen + ≥110 列时变成**侧边常驻 diff 面板**，随编辑自动刷新；`Ctrl+X B` 在"本次会话 / 本分支未提交 / 自分叉点以来"三种基线间循环；每个文件有 `ask` 按钮，把 diff 附到下一条 prompt。
- `Esc Esc` 打开 rewind 菜单，五选一：代码+对话 / 只对话 / 只代码 / 往后摘要 / 往前摘要。
- `Shift+Tab` 循环 6 档权限：`default` → `acceptEdits` → `plan` → `bypassPermissions` → `auto`。
- statusline 是**外部命令**：JSON 从 stdin 喂给脚本，脚本输出任意多行 ANSI。字段含 `context_window.used_percentage`、`cost.total_cost_usd`、`prompt_cache.hit_ratio`、`rate_limits.five_hour.used_percentage`、`pr.review_state`。不耗 token，300ms 防抖。

**解决什么**：长任务里三个具体恐惧——不知道在干什么（子代理面板 + 工具行）；不知道花了多少、还能跑多久（statusline）；不知道改错能不能退（rewind 五选一，而非只有一个"撤销"）。

**做得不好**【事实，[checkpointing](https://code.claude.com/docs/en/checkpointing)】

- checkpoint 只跟踪文件编辑工具，**Bash 改的文件（`rm`/`mv`/`cp`）不能回滚**；subagent 的编辑通常也不恢复。这是"看着能退、实际退不干净"的信任陷阱。
- todo 面板只显示 5 项，更多要靠对话问"show me all tasks"——把长任务又推回聊天。
- `/compact` 是"读全文再总结"的大请求，且重写缓存前缀，按 expected rebuild 计费。
- 33 个 hook 事件、6 档权限、5 种 handler：能力极强，心智负担已外溢到文档。

**搬到 wukong**

wukong 已有六档语义、`ACTIVITY_SNAPSHOT(PLAN)`、`interrupt` 审批、budget 条，缺的是"镜头"：

1. 权限 chip 点击弹菜单（不是下拉框），每项一行"哪些操作不再询问"；切换走 `POST /tasks/{id}/mode`，transcript 插一条 `mode changed` 分隔线。
2. `Esc Esc` 打开 `<dialog>` 列 checkpoint（序号 + 摘要 + 改动文件数），三按钮"代码/对话/两者"。后端已有 `test_rewind.py`，补 `GET /tasks/{id}/checkpoints` 返回 `{seq,label,files,can_restore_code}`。
3. plan 卡片不要埋在 transcript（滚两屏就看不见）：改成 topbar 下 40px progress strip，只显示当前步 + `3/7`，点击展开。

---

## 2. Codex CLI：权限模型要"可解释"，不是"可配置"

**UI**【事实，[cli](https://developers.openai.com/codex/cli)、[approvals](https://learn.chatgpt.com/docs/agent-approvals-security)】

启动是圆角框：`>_ OpenAI Codex` / `model: gpt-6.1-sol medium` / `directory: ~/code`，下面列 `/init`、`/status`、`/permissions`、`/model`、`/review`。输入行 `›`，底部固定 `100% context left · ? for shortcuts`——**上下文余量是常驻的一等公民**。

权限是二维矩阵：审批 `on-request`（离开沙箱/联网/不可信命令时停）或 `never`；沙箱 `read-only` / `workspace-write`（默认无网络）/ `danger-full-access`。可写根含 cwd 与 `/tmp`，但 `.git`、`.agents`、`.codex`、`.aws` **递归只读**；`/status` 列出当前工作区。`approvals_reviewer="auto_review"` 可把审批路由给 reviewer agent，失败关闭。

**解决什么**："我现在到底能干什么"——按一次键看到边界，而不是读配置。它把"沙箱 + 审批 + 可写根 + 网络"压成两个词（Auto / Read-only），比 Claude Code 克制。

**做得不好**

- `untrusted` 直接删除而非兼容，老配置升级即崩——内部整洁优先于用户。
- `workspace-write` 默认断网，大量任务第一次跑就卡在 `pip install`，用户不知道为什么。
- 官方 approvals 页**几乎不描述 TUI 如何渲染 diff 与审批弹窗**，只在版本控制建议里说"review diffs"。对比 Claude Code 用整页描述 diff 面板的列宽、跳转、基线切换，Codex 的 diff 不是一等公民。

**搬到 wukong**

侧栏"沙箱"目前是**状态展示**（seatbelt 开没开），不是**能力解释**。改成可点击 chip：

```
workspace-write · 网络关 · 可写 2 个根 · 受保护 3 个
```

点开 popover 逐条列：可写根（绝对路径 + 来源）、受保护路径及原因（`.git` 防篡改、`.env` 防泄密）、网络策略。数据从现有 sandbox 配置生成，无需新后端。**价值：审批弹窗里用户第一次能判断"是否越界"，而不是只看命令字面。**

---

## 3. Cursor：diff 必须长在代码上下文里

**UI**【事实，[agent](https://cursor.com/docs/agent/overview)、[rules](https://cursor.com/docs/rules)】

右侧 Agent 侧栏；编辑区 inline diff（新增绿/删除红，逐 hunk 接受）；聊天时间线有 checkpoint 圆点，点一下预览当时全部文件，再 Restore 回滚；`@` 引用文件/目录/符号/历史线程/分支 diff/URL；rules 四层：Team → Project（`.cursor/rules/*.mdc`，`alwaysApply`/`globs`/`description` 决定注入方式）→ User → `AGENTS.md`；工作中 `Enter` 入队、`Cmd+Enter` 插到下一次工具调用（steer），`/goal` 给长寿命目标。

**解决什么**：审查成本。聊天里贴 diff 不是"不好看"，是**失去代码上下文**：看不到 hunk 在函数里的位置、看不到相邻未改动行、不能跳定义。Cursor 的答案是 diff 留在 buffer，聊天只做导航。

**做得不好**【事实】

- checkpoint **只回滚文件，不回滚消息**（原文："reverts files only; it does not remove messages"）。用户以为回到过去，其实对话还记着错误路径，模型会沿错误前提继续推理。
- checkpoint 存本地、独立于 Git，换机器就没了；官方明确"不要当版本控制用"。
- rules 无大小限制，`alwaysApply: true` 进每次请求；团队规则还有优先级覆盖，排障时很难知道"到底生效没有"。

**搬到 wukong**

preview 面板已经会给 unified diff 上色（`app.js:891-909`），但它是**单文件预览**，不是**变更集**：

- 数据结构：`changeset = {files:[{path,status,additions,deletions,hunks:[{header,lines:[{t:'ctx'|'add'|'del',n,s}]}]}]}`，由 ledger 的 before/after 生成，不依赖 git。
- 交互：顶部文件列表（带 `+12 −3`）→ 点击滚到 hunk → 每个 hunk `接受/拒绝` → 底部"全部接受 / 把未接受的意见发给 agent"。
- 关键决策：**拒绝的 hunk 不落盘**。要求 approval 阶段就带 hunk 粒度，而不是先写再 diff。wukong 的"写前 ledger"正好支持——这是相对 Cursor 的结构性优势。

---

## 4. Cline / Roo / Kilo：计划/执行两阶段 + 快照

**UI**【事实，[plan-and-act](https://docs.cline.bot/core-workflows/plan-and-act)、[checkpoints](https://docs.cline.bot/core-workflows/checkpoints)】

侧栏 Plan/Act 开关（可给两模式配不同模型：Opus 规划、Sonnet 执行）。Plan 只读，Act 继承完整对话。每次工具调用后对话里出现书签 + 虚线连到 **Compare** 与 **Restore**；Restore 三选一：只恢复文件 / 只恢复任务（删消息留文件）/ 两者。`/deep-planning` 系统化探索并**先问澄清问题**再给计划。

**解决什么**："先校准方向，再花钱执行"。Plan 把最贵的错误（方向错的执行）挡在门外；checkpoint 把试错成本降到接近零，auto-approve 才敢开。

**做得不好**【事实】

- checkpoint 是**影子 git 仓库**，每次工具调用后 commit 当前全部文件。官方承认：大仓库显著占空间、拖慢 Cline。这是"每步全量快照"的必然代价。
- 恢复粒度是整个 checkpoint，不能只回滚一个文件——比 Zed 的 hunk 级粗一档。
- Plan/Act 是**模式**不是**产物**：计划留在对话里，不能编辑、不能勾选、不能作为契约被后续引用。

**搬到 wukong**

有 plan 事件，但没有"计划批准"这个门。加 `plan_approval` interrupt：

1. planner 出 steps 后 `RUN_FINISHED(outcome=interrupt)`，`interrupt.kind="plan"`，`plan.steps=[{id,description,tool_hint,risk}]`。
2. 前端渲染可编辑列表：拖排序、删除、改描述、标"跳过"；提交 `POST /resume` 带 `{decisions:{plan:[...]}}`。
3. 批准后 plan 事件带 `approved_at`，后续 `STEP_STARTED/FINISHED` 与之对齐。

**效果**：花第一个 token 前就能改方向；计划成为可追溯契约，而不是聊天里的一段字。

---

## 5. OpenHands：agent 干的活要"可接管、有证据"

**UI**【事实，[agent-canvas](https://docs.openhands.dev/openhands/usage/agent-canvas/overview)】

2026 年已改名 **Agent Canvas**，定位从"AI 软件工程师"变成"自托管多 agent 控制中心"：浏览器 UI 连一个或多个 backend（本机/Docker/VM/Modal/Cloud），左侧会话，右侧工作区（文件、终端），一个 conversation 属于一个 backend，可从任意消息 **branch**。支持 ACP，能跑 Claude Code、Codex、Gemini CLI、OpenCode。

**解决什么**："我不信它说跑通了，我要看证据"。文件树、终端输出、浏览器截图的本质是**把 agent 的声称变成可核对的现场**。branch 解决"这条路错了，但我想留原路"。

**做得不好**

- 本地 npm 安装时**关掉终端 backend 就停**，浏览器 UI 失联（官方 Warning）。对"控制中心"是架构级尴尬。
- Local GUI → Canvas 迁移留下两套入口（`agent-canvas` vs `openhands serve`），文档要专门解释"装了 canvas 就没有 serve"。
- 概念太多：backend/workspace/conversation/sandbox/automation/app，第一次打开不知道点哪。

**搬到 wukong**

wukong 不是 computer-use，不需要浏览器视图；需要**证据面板**。现在 `TOOL_CALL_RESULT` 一律塞 `<pre>` 截断 4000 字（`app.js:776-778`），读文件、跑测试、git diff 长得一样。按类型分流：

- `read_file`/`write_file` → 文件预览（高亮、行号、diff 着色），复用 preview。
- `shell` → 终端块：等宽、ANSI 颜色、退出码单独一行。
- `run_tests` → `passed/failed/skipped` + 失败用例列表，点击跳输出行。

纯前端映射，后端只需在 `metadata` 加 `kind` 与 `exitCode`。

---

## 6. Aider：闭环比界面重要

**UI**【事实，[repomap](https://aider.chat/docs/repomap.html)、[lint-test](https://aider.chat/docs/usage/lint-test.html)】

极简 REPL。启动横幅就把关键状态说清：`Git repo: .git with 258 files` / `Repo-map: using 1024 tokens`。每次改动自动 commit；`/undo` 回退；`/run` 执行并把输出问你要不要加进上下文。

repo map 值得抄：tree-sitter 抽类/函数签名 → 文件为节点、依赖为边建图 → **PageRank** 排序 → token 预算内选最重要片段。`--map-tokens` 默认 1k，但会随对话动态扩张。lint/test 闭环：`--auto-lint` 默认开，非零退出即错误；`--test-cmd` + `--auto-test` 每次编辑后跑，失败回灌让模型自己修。

**解决什么**："改完就算完"是 agent 最大的谎。Aider 把验证做成默认行为：不问用户要不要跑测试，而是跑完把失败喂回去。

**做得不好**

- repo map"通常守住 1k，但会显著扩张"——**成本不可预测**，扩张时机由内部启发式决定。
- 自动 commit 污染历史：一个任务十几个 `aider: ...`，review 要 squash。
- 无结构化 UI：工具调用、diff、测试结果是同一种文本流，长会话无法定位。

**搬到 wukong**

有 workflow、903 测试、shell，但没有"验证"这个一等公民。加 **verify 阶段**（不是新工具）：

1. 配置 `verify: {match:"src/**/*.py", command:"pytest -q", max_rounds:2}`。
2. 写操作批次结束后自动跑；失败把 stdout 尾部 200 行作为新 tool result 注入，继续一轮。
3. 发 `STEP_STARTED(verify)` / `STEP_FINISHED(verify,{passed,failed})`，前端渲染成独立步骤，带 ✓/✗ 与耗时。
4. pytest 全量输出折叠成 `pytest: 12 passed, 1 failed (3.4s)`，点开看失败用例。

---

## 7. Crush / OpenCode：产品感来自键位与主题

**UI**【事实，[crush](https://github.com/charmbracelet/crush)、[opencode](https://opencode.ai/docs/tui/)】

Crush：`ctrl+p` 命令面板、`ctrl+l` 模型选择器、会话选择器显示 `IsBusy` 与 `AttachedClients`；主题两个内置，`ctrl+e` 实时编辑、输入即预览、`esc` 回滚；`permissions allow/deny` 按工具名白/黑名单；多客户端共享 workspace（同 `--cwd` 共享会话、权限队列、LSP、MCP）。

OpenCode：`ctrl+x` leader key；`tui.json` 可配 `diff_style:"auto"|"stacked"`、`scroll_speed`、`mouse`、`attention`（question/permission/error/done/subagent_done 五种声音 + 失焦才发通知）；`/undo` `/redo` 用 git 管文件，因此**必须是 git 仓库**；`/export` 导出 Markdown 并用 `$EDITOR` 打开。

**解决什么**：肌肉记忆。命令面板让"记不住命令"消失；主题实时预览让"改坏能不能退"消失；attention 让"跑完我不知道"消失。

**做得不好**

- Crush 只有 2 个内置主题，README 完全不描述 diff 怎么显示——视觉是卖点，diff 是黑盒。
- OpenCode 的 `/undo` 依赖 git，非 git 目录直接失效；`diff_style` 只有两档。
- 能力大量塞进键位，**没有可发现性**：不看文档不可能知道 `ctrl+x c` 是 compact。

**搬到 wukong**

`style.css` 开头的六条设计原则（居中栏、背景层级、单一 accent、三级边框）已经比多数开源项目讲究。缺两样：

- `data-density="comfortable|compact"` 挂 `<html>`，一套 CSS 变量控制行高/间距/字号；compact 下工具行 24px，一屏 30 步。
- 键位表驱动：`KEYMAP=[{keys:["mod+k"],id:"palette"}]`，`?` 按上下文过滤后弹出。

---

## 8. Devin / Manus / Amp：可信感 = 可验证进度 + 可接管 + 跨端连续

**Devin**【事实，[session-tools](https://docs.devin.ai/work-with-devin/devin-session-tools)】

Progress tab 把 shell、编辑、浏览器活动合并成**一条时间线**；IDE/Browser/Shell 三 tab 可实时看它改代码；Side Chat 只读，`/btw` 触发；接管要先 **stop session**，改完 resume 并告诉它你改了什么；命令历史里"未来"的命令是灰的，点一下跳到那个时间点。

**Manus**【事实，[泄露 prompt](https://gist.github.com/jlia0/db0a9695b3ca7609c9b1a08dcbf872c9)】

`todo.md` 是显式产物：Planner 给编号计划，todo.md 记更细清单，**每完成一项立即改标记**，计划大变时重建。消息工具两档：`message_notify_user`（非阻塞，进度用）与 `message_ask_user`（阻塞，只在必要时）。首条回复只确认收到，不给方案。

**Amp**【事实，[docs](https://ampcode.com/manual)】

同一 thread 跨 Web/macOS/iOS/CLI；每个 thread 一台 **Orb**（预装代码、工具、插件），合上笔记本继续跑；"用不到的功能就删，不做向后兼容"。

**解决什么**：Devin 解决"它在另一台机器上，我怎么知道不是瞎编"；Manus 解决"别每步都问我"；Amp 解决"换设备不想丢上下文"。

**做得不好**

- Devin 接管必须停：文档明确"确保 Devin 已暂停再接管"。所谓人机协作其实是**人机交替**。
- Manus 的 `ask` 一发就阻塞整个 loop；问题问得不好，用户回来已浪费几分钟。
- Amp 明确 no backward compatibility：对个人是优点，对团队是升级即断。

**搬到 wukong**

现在只有**阻塞审批**，没有**非阻塞通知**。加 `CUSTOM notify`：

- 后端 `notify(level,text)` → `CUSTOM {name:"notify", value:{level,text,at}}`。
- 前端不进 approval 队列，渲染成一行 dim 文本（`· 已读 12 个文件，准备跑测试`），保留最近 3 条。
- 规则：进度用 notify，只有"写文件/花钱/越界"才 approval。这与 Manus 同源，但六档权限能做得更细。

---

## 9. AG-UI + CopilotKit：前端不该猜 agent 状态

**协议**【事实，[events](https://docs.ag-ui.com/concepts/events)】

五族事件：

- 生命周期：`RUN_STARTED` / `RUN_FINISHED`（`outcome` 可为 `{type:"interrupt",interrupts:[...]}`）/ `RUN_ERROR` / `STEP_STARTED` / `STEP_FINISHED`。
- 文本与工具：`TEXT_MESSAGE_START` → `CONTENT(delta)` → `END`；`TOOL_CALL_START` → `ARGS(delta)` → `END` → `RESULT`。
- 状态：`STATE_SNAPSHOT`（整体替换）/ `STATE_DELTA`（RFC 6902 JSON Patch）/ `MESSAGES_SNAPSHOT`。
- 活动/推理/子代理：`ACTIVITY_SNAPSHOT/DELTA`（`activityType` 如 `PLAN`）、`REASONING_*`、`SUBAGENT_STARTED/FINISHED/ERROR`。

所有事件共享 `type`、`timestamp`、`metadata`（token 用量、trace id，按 key 合并后写覆盖）、`subagentRunId`（溯源不是归属，状态仍只有一份）。

**解决什么**：断线重连与中途加入。快照给基线、增量省带宽，前端不必"重放全部事件才知道现在是什么状态"。

**做得不好**

- 事件种类多，实现者极易只做子集。**wukong 就是活例子**：`agui.py:154-164` 定义了 `step_started/step_finished`，但 `app.js` 的 `renderEvent` 没有 `STEP_STARTED` 分支——事件发了，前端丢了。
- `ACTIVITY` 不回传给 agent，意味着"计划"是纯展示，不能作为下一步的输入契约。
- CopilotKit 的 `useCoAgentStateRender` 有状态更新不一致的 issue（#2931），说明 snapshot/delta 的客户端实现仍有坑。

**搬到 wukong**

1. 补齐前端事件：`STEP_*`（每步计时与"当前步骤"）、`STATE_DELTA`（大任务增量更新 plan）。
2. 用 `SUBAGENT_*` 做扇出：`multi_agent` 已有，AG-UI 层没发子事件。给所有子事件加 `subagentRunId`，前端按 `parentToolCallId` 建树。
3. 断线恢复发 `MESSAGES_SNAPSHOT` + `STATE_SNAPSHOT`，不重放。后端已有 `replay()`（`app.py:801`），加 `(task_id,last_seq)` 快照缓存即可。

---

## 10. Zed / Warp：review 是主循环，不是收尾

**Zed**【事实，[agent-panel](https://zed.dev/docs/ai/agent-panel)】

编辑后出现**折叠条**：改了哪些文件、几个文件、多少行。点它或 `Ctrl+Shift+R` 打开 **multi-buffer** 汇总，**逐 hunk accept/reject**；`agent.single_file_review=true` 时 diff 内联到各文件。每条消息顶部有 "Restore Checkpoint"。模型选择器旁显示 token 用量。`Ctrl+Alt+J` 开 Threads 侧栏（按项目分组），`Ctrl+Tab` 循环。"Follow the Agent" 让编辑器跳到 agent 正在碰的文件。

**Warp**【事实，[interactive-code-review](https://docs.warp.dev/agents/local-agents/interactive-code-review)】

Code Review 面板聚合 agent 的所有改动；可在**任意行/块留评论**，批量攒着一次提交；agent 改完返回新 diff，循环。要求目录是 git 索引的。Agent Management Panel 管并行会话；blocks 可作上下文。

**解决什么**：审查不是收尾动作，而是**每几轮发生一次**的主循环。所以必须便宜：能在 diff 上直接说"这行不对"，而不是切回聊天描述"第 42 行那个 if"。

**做得不好**

- Warp 依赖 git index；wukong 这种"写前 ledger"反而没这个限制。
- Zed 的 external agent 不支持 checkpoint/token（官方注明"取决于集成"）——协议不统一导致能力碎片化。
- 两者都要求"先有 diff 才能评论"，对"还没写、只想换方案"无解。

**搬到 wukong**

preview 升级为 **Review 面板**（三态 `changeset`/`file`/`comment`）：顶部 `3 个文件 · +48 −12` + "全部接受"；中部文件 accordion，每 hunk 左侧 checkbox（默认勾选=接受）；点击行号出现评论框，存 `{path,line,side,text,status:'open'}`；底部"发送 N 条评论"把 open 评论拼成结构化 JSON（不是自然语言），agent 能精确定位。这是 Warp 批量评论 + Zed hunk 级 accept 的合体，且因有 ledger，**未接受的 hunk 可以根本不写盘**。

---

## 11. 七条设计定律

1. **进度要结构化，不要日志化。** 用户不需要 200 行 JSON，需要"第 3/7 步、正在读 app.py、已花 12s"。Claude Code 的 todo、Devin 的 Progress、Manus 的 todo.md 是同一件事的三种形态。
2. **审批要分级且可解释。** 六档不是给用户选的，是给场景选的；界面永远只显示"当前档位 + 这一档意味着什么"。Codex 的 `/permissions` 是正面，33 个 hook 是反面。
3. **diff 是主界面，不是附件。** 判断标准：能否不切回聊天完成一次审查。Cursor/Zed/Warp 做到了，Aider/OpenHands 没做到。
4. **计划必须能被批准和编辑。** 只读 plan 卡片是装饰；能改步骤、能跳过、能作为契约，才省 token。
5. **恢复、分支、回滚是三件事。** 恢复=继续；分支=留原路试新路；回滚=撤副作用。混为一谈就出 Cursor 那种"文件回滚了、对话还记着"的问题。
6. **键位与命令面板决定专业感。** `Ctrl+K`/`/`/`@`/`!`/`?` 让重度用户手不离键盘，直接决定留存。
7. **流式渲染必须 O(n)。** 任何"每 delta 全量重解析 Markdown"的实现，5000 字以上必掉帧。纯工程问题。

---

## 12. wukong 现状诊断（基于代码）

**已经不错的**

- 三栏布局与设计令牌（`style.css:1-60` 的六条原则、三级边框、单一 accent）比多数同类成熟。
- AG-UI 编码层覆盖 RUN/TEXT/TOOL/STATE/ACTIVITY/CUSTOM，`interrupt` 用 `RUN_FINISHED.outcome`，符合协议。
- 审批卡片已给 unified diff 上色（`app.js:891-917`）；另有 budget bar、plan 卡片、工具行折叠 + 一行 gist、写前 ledger、六档权限——**内核概念比 UI 领先**。

**明确的缺口**

| 缺口 | 证据 | 后果 |
|---|---|---|
| `STEP_*` 发了前端不接 | `agui.py:154-164` 有定义；`app.js:689-834` 无分支 | 无每步耗时、"当前在做什么" |
| 无 subagent 事件 | `agui.py` 无 `subagent_*`；`multi_agent.py` 存在 | 扇出在 UI 上不可见 |
| 无会话列表 | `app.py:404` 有 `GET /sessions`；侧栏只有"任务" | 历史不能按会话恢复 |
| 无 rewind UI | `test_rewind.py` 存在 | 回滚能力用户看不见 |
| 无 changeset / hunk 审查 | approval 是单块 `preview` | 只能整体批准/拒绝 |
| 无成本/时长常驻 | `app.py:809` 有 `cost_usd`，只在详情 | 长任务不知花多少 |
| 无命令面板 | 只有 Enter/Shift+Enter | 重度用户效率低 |
| Markdown 每 delta 全量重渲染 | `app.js:712-720` | O(n²)，长消息掉帧 |
| 无虚拟化 | CSS 无 `content-visibility` | 长 transcript 滚动卡 |
| 压缩只有一行 dim 文案 | `app.js:857` | 不知压缩掉了什么 |

---

## 13. 优先级改进清单

### P0：不做就显得业余

#### P0-1 变更集与 hunk 级审查

- **改什么**：审批从"一段 preview"升级为 `changeset`；右侧 Review 按文件分组、按 hunk 勾选；拒绝的 hunk 不写盘。
- **为什么**：整体批准 = 用户放弃审查；没有 hunk 粒度，diff 只是装饰。这是"agent 能否被信任"的核心。
- **预期**：5 文件的任务 30 秒审完，只回退其中 2 个 hunk；误改率显著下降。
- **实现**：`interrupt` 带 `{files:[{path,status,hunks:[{id,header,lines:[{t,n,s}]}]}]}`，由 ledger before/after 生成；单 hunk >200 行截断，展开走 `GET /tasks/{id}/changeset/{path}` 懒加载；返回 `{decisions:[{path,hunkIds,action}]}`，runtime 只应用 accept；二进制只显示"已修改"。
- **风险**：大 diff 几 MB → 分页 + 服务端截断；hunk 边界与写入偏移不一致 → 用"原文匹配 + 上下文 3 行"而非行号定位。

#### P0-2 每步计时 + "当前在做什么"条

- **改什么**：接 `STEP_STARTED/FINISHED`；工具行显示耗时；composer 上方 sticky 条：`正在读取 app.py · 12s · 第 3/7 步`。
- **为什么**：日志只能回答"过去"，不能回答"现在"；长任务第一焦虑就是"它是不是卡了"。
- **预期**：不用翻 transcript 就知道进度；>30s 的步骤显示"仍在运行"，减少误取消。
- **实现**：`TOOL_CALL_START` 记 `performance.now()`，`RESULT` 算差值写 header；状态条从"最后一个 running tool + 当前 plan step"派生；单个 `setInterval(1000)` 只改活动节点，数字用 `tabular-nums`。
- **风险**：频繁 DOM 更新 → 只改文本节点；后台标签页节流 → 用 `Date.now()` 差值而非计数。

#### P0-3 会话/任务历史与恢复

- **改什么**：侧栏"任务"改"会话 → 任务"两级；显示最后活跃、任务数、状态、累计成本；点击只读回放 + "继续"。
- **为什么**：现在刷新就只剩当前任务，用户不敢关标签页。历史是"本地优先"的信任基础。
- **预期**：关浏览器再回来，2 次点击回到昨天任务并 resume。
- **实现**：`GET /sessions` 已有，SQL 聚合补 `last_active_at`/`task_count`/`status`/`cost_usd`；点击走 `GET /tasks/{id}` 拿 snapshot（`app.py:801`）；回放时禁用 composer，顶部显示"只读 · 时间"；"继续"调 `POST /resume`。
- **风险**：大任务 replay 慢 → `(task_id,last_seq)` 快照缓存；>5000 事件只回放尾部。

#### P0-4 命令面板与键位层

- **改什么**：`Ctrl/Cmd+K` 面板；`/` 命令（compact/rewind/clear/model/export/plan）；`@` 文件模糊；`!` shell；`?` 帮助；`Esc` 中断。
- **为什么**：这是"工具"与"网页"的分界线。重度用户一天几百次按键，鼠标是税。
- **预期**：常用操作从"找按钮"变 2 次按键；新用户按 `?` 能发现全部能力。
- **实现**：`KEYMAP` 表 + `<dialog>`；命令注册 `{id,title,keys,when,run}`，`when` 按状态过滤（有审批时只显示审批命令）；`@` 复用 file tree API；`!` 走 approval 而非直接执行。
- **风险**：输入法冲突 → 复用 composer 的 `composing` 标志（`app.js:1510-1517`）；`Cmd+K` 被浏览器占用 → 面板内可改键。

#### P0-5 流式渲染性能

- **改什么**：delta 入 buffer，rAF 合并；只重解析已闭合块，最后一块保持文本节点；`.turn` 加 `content-visibility:auto; contain-intrinsic-size:0 240px`。
- **为什么**：现在每个 delta 都 `replaceChildren(renderMarkdown(整个 raw))`（`app.js:712-720`）。8000 字回复按 1 字 delta 到达 = 8000 次全量解析，O(n²)。
- **预期**：长回复稳定 60fps；100 轮 transcript 滚动不掉帧。
- **实现**：`pending += delta`；rAF 里若未闭合围栏则整块重渲染，否则 append 到最后一个文本节点；块边界用空行/``` 判断。
- **风险**：边界判断错导致闪烁 → 保守策略：未闭合 ``` 或表格整块重渲；Safari find-in-page 兼容 → 提供开关。

### P1：做了明显更强

| # | 改什么 | 为什么 / 预期 | 实现要点 | 风险 |
|---|---|---|---|---|
| P1-1 | 计划批准门 | 执行前校准，省 token；计划成为契约 | `interrupt.kind="plan"` + 可编辑步骤 + resume 带 decisions | 与 planner 状态不一致 → 批准后重放 plan |
| P1-2 | subagent 树 | 扇出不可见，用户以为卡住 | `SUBAGENT_*` + `parentToolCallId` 建树 | 20 并发太深 → 默认折叠 |
| P1-3 | rewind 时间线 | 能力已有但不可见 | 事件序号 → checkpoint 节点，三选一 | Bash 副作用不可回滚 → 标注"仅文件编辑" |
| P1-4 | 成本/上下文 statusline | 不知花多少、还能跑多久 | `cost_usd` + context 阈值变色（70/90%） | 估算误差 → 标"估算"并给原始 token |
| P1-5 | 验证闭环 | 改完自动跑测试并回灌 | verify 阶段 + `STEP_*` + 折叠输出 | 测试慢 → 超时降级为建议手动 |
| P1-6 | 权限切换 + 能力边界 chip | 六档已有但用户不知道能干什么 | topbar chip + popover | 语义复杂 → 每档一句人话 |

### P2：锦上添花

| # | 改什么 | 说明 |
|---|---|---|
| P2-1 | 图片粘贴 | `Ctrl+V` 插图 → `[Image #1]` chip；Codex/Zed 已证明是刚需 |
| P2-2 | 导出 Markdown | transcript + 工具结果 + diff 导成单文件，便于复盘与提 issue |
| P2-3 | 通知与声音 | 失焦才发通知；完成/审批/错误三音；OpenCode 的 `attention` 是范本 |
| P2-4 | 密度/主题 | `data-density` + 两套主题；compact 一屏 30 步 |
| P2-5 | 并行会话视图 | 多任务并排；Amp/Devin 形态，需后端支持多 run 同屏 |

---

## 14. 结论

wukong 的内核（事件溯源、ledger、六档权限、预算压缩）已经比它的界面先进一代；**最大的浪费是这些能力在 UI 上不可见**。P0 五件事——变更集审查、步骤计时、会话历史、命令面板、流式性能——没有一件需要新架构，全部是在现有 AG-UI 事件与 SQLite 事件流上补"最后一公里"。先做这五件，再谈 subagent 树与验证闭环。

---

### 参考

- Claude Code：[interactive-mode](https://code.claude.com/docs/en/interactive-mode) · [hooks](https://code.claude.com/docs/en/hooks) · [sub-agents](https://code.claude.com/docs/en/sub-agents) · [statusline](https://code.claude.com/docs/en/statusline) · [checkpointing](https://code.claude.com/docs/en/checkpointing) · [permission-modes](https://code.claude.com/docs/en/permission-modes) · [costs](https://code.claude.com/docs/en/costs)
- Codex：[CLI](https://developers.openai.com/codex/cli) · [approvals](https://learn.chatgpt.com/docs/agent-approvals-security) · [commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- Cursor：[agent](https://cursor.com/docs/agent/overview) · [rules](https://cursor.com/docs/rules)
- Cline：[plan & act](https://docs.cline.bot/core-workflows/plan-and-act) · [checkpoints](https://docs.cline.bot/core-workflows/checkpoints)
- OpenHands：[Agent Canvas](https://docs.openhands.dev/openhands/usage/agent-canvas/overview)
- Aider：[repo map](https://aider.chat/docs/repomap.html) · [lint & test](https://aider.chat/docs/usage/lint-test.html)
- Crush：[GitHub](https://github.com/charmbracelet/crush) · OpenCode：[TUI](https://opencode.ai/docs/tui/)
- Devin：[session tools](https://docs.devin.ai/work-with-devin/devin-session-tools) · Amp：[docs](https://ampcode.com/manual) · Manus：[泄露 prompt](https://gist.github.com/jlia0/db0a9695b3ca7609c9b1a08dcbf872c9)
- AG-UI：[events](https://docs.ag-ui.com/concepts/events)
- Zed：[agent panel](https://zed.dev/docs/ai/agent-panel) · Warp：[interactive code review](https://docs.warp.dev/agents/local-agents/interactive-code-review)
- 第三方：[Armin Ronacher, Agentic Coding](https://lucumr.pocoo.org/2025/6/12/agentic-coding/)
