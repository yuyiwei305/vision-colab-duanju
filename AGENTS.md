# Vision Media · 执行入口

## 协作与授权

- 中文、简洁；核对前提，区分事实、推测与未验证项。无问题的检查回复 `OK`。
- 用户下达任务即授权连续完成范围内工作。遵守明确停点、预算、数量和保留项；材料与当前 SOP 冲突时先说明并等待取舍。详见[连续执行 SOP](docs/autonomous-execution-sop.md)。
- 未经明确允许，不用子 Agent，不充值、不公开发布、不永久删除、不对外发消息。
- 只改任务需要的内容。允许按授权修正共享架构，不为保留旧实现而扭曲创作概念；保留既有素材、任务记录与无关改动。
- 编码按需使用 Matt Pocock 技能；测试验证真实行为，避免无关全量测试、XcodeBuild 或模拟器。

## 按总 SOP 执行

制作顺序、交接和退回位置以[18 步总 SOP](docs/剧集制作总SOP.md)为准。每项任务先读[项目技能入口](.agents/skills/vision-media-workflow/SKILL.md) → [short-drama](ref/drama-skills/skills/short-drama/SKILL.md) → 当前阶段技能及必需资料。核实既有产物后从适用步骤接续，做到用户授权的交付范围。运行 `bash scripts/drama skills --step N` 读取当前步骤的技能正文和必读资料。

| 总 SOP | 执行入口与交接 |
| --- | --- |
| 1–2 收本与立项 | `short-drama` 核原稿、范围和已有成果，通过 `project`、`episode spec` 确认对应规格。 |
| 3–4 开发与剧本 | `short-drama-develop/write` 加载对应剧作资料。已有本地编剧指南的工作区同时加载该指南。正式剧本写清场次、时间关系、关键变化和声音职责。 |
| 5 资产与音色设计 | `short-drama-assets` 维护 `视觉设定.md`。各时段的造型、场景和道具状态注明生效场次。 |
| 6 分镜 | `short-drama-storyboard` 按 [`storyboard-logic`](.agents/skills/storyboard-logic/SKILL.md#分镜) 通读目标集完整剧本，先排跨场跨幕，再排场内镜头与按风险预演。H3 设计前加载 `forest-laoli-h3`。 |
| 7–8 生图与冲击帧 | `short-drama-image-prompts → short-drama-produce`。按[生图 SOP](docs/image-generation-sop.md)登记和验收实图，消费镜头就绪后回绑。冲击帧按分镜需求启用。 |
| 9–12 视频提示词、投产、取回与审查 | `short-drama-video-prompts/review/produce` 用同一份分镜交接。H3 提示词和审查加载 `forest-laoli-h3`，在第 10、12 步按 `storyboard-logic` 复核。通过工厂冻结、运行、取回和释放。 |
| 13–17 剪辑、增强、后期与成片验收 | `short-drama-edit/review` 按[剪辑 SOP](创作者决策/剪辑SOP.md)锁画、按需增强、完成字幕与混音，回看实际导出。 |
| 18 交付与归档 | `short-drama/edit` 核对最终版本、工程依赖、跨集继承及续做索引。 |

阶段工单以正式文档、实图、原声或实际导出为证据，分别记录结构检查、实际观察、执行者选用和用户确认。自检如实标注。发现缺陷按[总 SOP 退回路径](docs/剧集制作总SOP.md#统一检查修订与恢复)回对应负责人，只复核受影响的下游。

<a id="h3-shot-segment-contract"></a>

## 镜头与生成段

- **Sequence → Motion → Shot。** Sequence 是段落，Motion 是一次 H3 生成，Shot 是该 Motion 内的镜头；请求时长只约束 Motion。
- 无切点跨 Motion 时读取已生成的实际音视频潜变量。Shot 在同一 Motion 内切镜时在正文写时点。跨时空跳转另起 Sequence 与独立 Motion，匹配切和声桥按[跨幕交接](.agents/skills/storyboard-logic/references/scene-transitions.md#写回分镜与沿用下游交接)交剪辑。
- 分镜、提示词、投产和剪辑必读[段落契约及示例](docs/h3/shot-motion-contract.md)；术语见 [CONTEXT.md](CONTEXT.md)。生成请求、去重净片、计划剪用和实际剪辑入出点分别核算。

## 作品与统一执行

- 新作品用官方 `project init` 放入 `作品/<作品>/`；续做读本作品配置和 `创作者决策/项目约定.md`，记录授权、范围、明确确认点、真实进度与下一步。
- 创作事实只在正式 Markdown；生产状态只在官方账本。新集只增加正文、参数和素材，**不写单集专属基础脚本**。
- 统一使用 `bash scripts/drama`。新 H3 或增强任务先按[工厂手册](docs/factory/README.md#7-发布迁移与能力边界)核对应执行包，旧任务按锁定快照续做。API 保留 prepare → confirm → run，由执行者按当次授权登记。
- 分配、预算和释放只管理本任务的实例。报错、掉线或取回失败时先查原请求并尝试恢复，保留可恢复实例，受理未知不重复提交。活动批次冻结正式输入。各实例完成自身范围并取回校验后及时释放并核实。明确停止或达到硬上限时停止新操作并尽力保全。

<a id="reference-video-mode"></a>
<a id="visual-pacing"></a>

## 媒体硬规则

- 新视频统一参考生视频；H3 用 V4 Ref2VA，原生 1344×768／768×1344、24fps。起止状态写文字，不用首尾帧硬锁。人物、场景、道具与构图参考按[H3 专项流程](docs/h3/forest-laoli-workflow.md)处理。
- 生图默认内置 image_gen；人物每造型一张组合图（三视全身遮面＋清晰大头照）。剪辑默认 ChatCut。工具不可用先说明具体限制，按对应 SOP 处理。
- 额外镜头图只为冲击帧制作或复用。场面须承担关键视觉冲击，且文字与现有资产不足以讲清画面关系。是否作为 H3 构图参考按[冲击帧规则](docs/h3/shot-motion-contract.md#冲击帧与-h3-构图参考)判断。钩子和局面变化按本集或作品节奏档案规划，按[节奏 SOP](docs/visual-pacing-sop.md)验实际成片。
- H3 音色优先复用本作品同角色已听核的 WAV；需新设计时默认 [Breeze TTS 2 音色 SOP](docs/Breeze-TTS-角色音色SOP.md)：先按人物设计，在 Colab L4 自托管生成样音，取回释放、听核后绑定。H3 同次生成对白，WAV 只参考音色。
- H3 开机前完成冻结输入和本任务总预算，按实际 MOTION 排产，整条续接链不拆实例。当前最多 2 台 G4。音色 L4 和成片增强各按专项流程准备及核算。[H3 并发规则](docs/project-rules.md#031-h3-多-colab-实例并发规则2026-11-09)
- 新增强默认用 ComfyUI ReCaVSR 整片一次等比超分至 4K，用 RIFE 补到 60fps 并保留原声。创作者指定的尺寸与帧率优先。按[增强 SOP](docs/成品画质增强SOP.md)执行。

执行参数、共用资产与凭证规则见[项目专项规则](docs/project-rules.md)。完成时回报实际改动、验证范围与尚未验证项。
