---
name: forest-laoli-h3
description: Vision Media 的 H3 V4 Ref2VA 专项技能。由项目入口在写改 H3 分镜、视频提示词或做 H3 专项审查时强制调用，提供站位、表演、声画和提示词诊改方法。
license: MIT
---

# 漫剧老李 · H3 V4 专项

本项目版取材自 forest-laoli-h3 v2.1.1（`dbbf67f62fe12ca2d135dab32cb64a54c19cb12b`）。
由 `short-drama` 路由，正式内容归当前阶段 owner。此处只补 H3 导演与诊改方法。

角色新音色默认走 [Breeze TTS 2 Colab SOP](../../../docs/Breeze-TTS-角色音色SOP.md)：先按人物设计、取回释放并听核，再绑定 H3 参考音色；既有合格身份 WAV 复用。旧任务的原生取样依赖只按原快照恢复，不作为新任务默认。

| 当前阶段 | 必读 | 应用位置 |
|---|---|---|
| `storyboard` / `short-drama-storyboard` | [调度与表演](references/storyboard.md) | 场景站位、工作侧、主动作、反应、切点和续接状态，写入 `分镜.md` |
| `prompts` / `short-drama-video-prompts` | [Ref2VA 正文](references/prompts.md) | 按真实绑定写六段，清理画面描述并核对声音时间线，写入 `视频提示词.md` |
| `review` / `short-drama-review` | [专项自检](references/review.md)、上面两份参考 | 对照正式来源定位风险；有真实视频才写观察结论 |

写改或审查打斗时，执行当前阶段参考中的打斗小节。

在对应阶段开始前，运行 `python3 scripts/h3_production.py skill --stage <storyboard|prompts|review>` 并实际读取输出。相同版本在本会话可以复用。交付或续做时在原索引记录实际使用的技巧、对应 ID 和检查结果。仅做单阶段工作就只记录该阶段。新批次生产前需有当前输入的三阶段记录；已有正式文档可用相应技能核对复用，按发现回到 owner 修订。

遵守 V4 参考生视频：每段 4–15 秒请求，按本段覆盖的镜头绑定真实参考图，编号按本段实际顺序，容量见[运行清单](../../../docs/h3/workflow-v4-g4-runtime.json)。同场连续机位或运镜接真实 AV latent。镜头起止写状态文字。额外构图图只用于完全独立 Motion 起镜中符合[冲击帧条件](../../../docs/h3/shot-motion-contract.md#冲击帧与-h3-构图参考)的画面。语速、反应、运镜及停顿按剧情和净时长选择。若必要方法与正式项目规则冲突，以项目规则为准。
