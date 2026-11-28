---
name: vision-media-workflow
description: Vision Media 的必经项目入口：按任务范围加载 short-drama 和当前阶段技能，执行统一工厂及真实交接检查。
---

# Vision Media 项目入口

1. 应用 [AGENTS.md](../../../AGENTS.md)，读取[连续执行 SOP](../../../docs/autonomous-execution-sop.md)；用[总 SOP](../../../docs/剧集制作总SOP.md)定位本次阶段，不预加载无关阶段。
2. 实际读取 [short-drama](../../../ref/drama-skills/skills/short-drama/SKILL.md)，由它路由本次 `short-drama-*` 技能及必要 references。仅维护规则也走主入口，不扩成作品制作。已读且版本未变可复用。
3. 涉及作品时，读其配置与既有 `创作者决策/项目约定.md`，沿用授权、明确确认点、预算和停止决定。材料与 SOP 冲突按连续执行 SOP 等待取舍。
4. 正文写回对应 owner 的正式文档；阶段交接核实际输入、产物和证据，不以“技能已读”、文件存在或结构通过代替质量验收。

## 按阶段加载

运行 `bash scripts/drama skills --step N` 并读输出。该入口覆盖总 SOP 1–18 步，并列出依赖宿主的工具。按阶段技能再读取与当前材料类型相关的按需资料。

- **写作**：第 3、4 步分别加载 `short-drama-develop`、`short-drama-write` 及相应剧作参考。原工作区有本地 `charlie-screenwriting-guide` 时同时读取其完整指南。按本集承诺定场次、情绪与时长。
- **生图**：`short-drama-assets → image-prompts → produce → imagegen`，读[生图 SOP](../../../docs/image-generation-sop.md)和[技法 SOP](../../../docs/image-craft-sop.md)。真实看图后选用回绑，人物组合图遵守项目版式。涉及作品间共用角色时，按本地登记核身份与版本。
- **分镜**：`short-drama-storyboard` 负责《分镜.md》，按 [`storyboard-logic`](../storyboard-logic/SKILL.md#分镜) 的顺序执行一次。第 10、12 步用该技能对应章节复核同一分镜。[18 步总 SOP](../../../docs/剧集制作总SOP.md#6-设计分镜空间关系和剪用节奏)
- **H3 分镜／提示词／审查**：进入对应阶段即加载 [forest-laoli-h3](../forest-laoli-h3/SKILL.md)，运行 `python3 scripts/h3_production.py skill --stage storyboard|prompts|review` 并读输出。分镜在排时长前读[段落契约](../../../docs/h3/shot-motion-contract.md)，将 **Sequence → Motion → Shot**、声音与去重净时长纳入同一次设计。
- **H3 生产**：先读[专项流程](../../../docs/h3/forest-laoli-workflow.md)，完成续接规划、三阶段检查、参考图验收与任务预算，再走工厂 prepare/run。新 H3 用 V4 Ref2VA；角色音色按 [Breeze TTS 2 SOP](../../../docs/Breeze-TTS-角色音色SOP.md)设计和绑定。实例按原任务恢复和释放。
- **剪辑／导出**：先读 `short-drama-edit` 和[剪辑 SOP](../../../创作者决策/剪辑SOP.md)，默认 ChatCut；本地 ffmpeg 不代替其主交付。
- **增强**：读[成品画质增强 SOP](../../../docs/成品画质增强SOP.md)。新任务默认 ComfyUI ReCaVSR 整片一次等比超分至 4K，用 RIFE 补到 60fps 并保留原声。

## 统一执行与覆盖

- [工厂入口](../../../docs/factory/README.md)管理单集规格、阶段工单、排产、总预算、恢复和交付。新集只写正式正文、参数和素材，不新增单集运行脚本。
- 分镜用 `Sequence → Motion → Shot`。Motion 归段落，Shot 归生成段；长镜在 Motion 之间用潜变量续接。格式见上述契约。
- `ref/drama-skills/` 是基于上游的项目定制唯一实现，已按授权修改并登记锁；不为保持上游旧限制另做覆盖实现或复制技能。
- 上游通用确认、停止及首尾帧分支按[连续执行 SOP 覆盖表](../../../docs/autonomous-execution-sop.md#6-技能条款覆盖表)处理。新视频统一参考模式，起止状态用文字。额外镜头图只按[冲击帧规则](../../../docs/h3/shot-motion-contract.md#冲击帧与-h3-构图参考)制作和绑定。
- 生成期间优先取回、校验和释放。等待运行结果时可整理独立审查草稿，活动输入保持冻结。交付可剪片段时，释放相应实例后运行 `bash scripts/drama skills --step 12`，按[片段审看](../../../docs/factory/README.md#motion-review)带声核对拟用区间和相邻镜头，再将需补拍的部分计入原任务预算。
- 收尾在作品原索引记录实际技能链、正式产物、检查结果与下一步；主观采用、执行者选用和技术成功分别记录。未涉及作品的规则维护不写入作品状态。

## 参考生视频的阶段覆盖

分镜用已验收的人物、地点、道具与风格参考，写清职责、关系和起止状态。提示词字段「图生视频」在本项目表示参考生视频，图片用 `reference_image`。起止用文字，连续画面用 AV latent 接续，构图参考按冲击帧条件启用。
