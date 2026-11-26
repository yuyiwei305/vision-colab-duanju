---
name: short-drama-produce
description: 在任务授权范围内，执行短剧项目的图片、视频、TTS/配音或时间线音乐生产任务，并把结果与精简运行记录存回项目。用户说“生成这张图/这段视频/这句配音/这段配乐”“开始跑图/跑视频/合成语音/生成音乐”“把已确认提示词送去生产”，或要求批量执行已授权媒体任务时使用；不负责创作提示词、镜头、台词、歌词或声音身份。
license: MIT
---

# 短剧媒体生产

本技能把已经写好的生产规格送到运行环境配置的 adapter。图片提示词归
`$short-drama-image-prompts`，视频提示词归 `$short-drama-video-prompts`，台词归
`$short-drama-write`，声音身份归 `$short-drama-assets`。

## Vision Media 生产入口

| 产物 | 当前入口 |
| --- | --- |
| H3 视频原片 | [工厂手册](../../../../docs/factory/README.md#3-h3-统一排产与总预算)。`episode plan → task prepare → task run/resume`，首次先登记范围、总预算和期限。按 [段落契约](../../../../docs/h3/shot-motion-contract.md) 对每个 MOTION 编译，连续长镜用真实 AV latent。 |
| 地点、人物、道具及冲击帧 | [生图 SOP](../../../../docs/image-generation-sop.md)。取正式 IMG 条目，经 `asset call` 登记，实际出图、验收再回绑。 |
| H3 角色音色 | [Breeze TTS 2 音色 SOP](../../../../docs/Breeze-TTS-角色音色SOP.md)。复用已听核 WAV 或先设计新音色，H3 同次生成当前表演与对白。 |
| 配乐或单独补录 | 仅在任务含该交付时，按已选适配器和正式台词或音乐章节执行。 |

[连续执行 SOP](../../../../docs/autonomous-execution-sop.md#2-预览与登记)规定预览、登记和恢复。输入变化后刷新受影响的快照，用户指定确认点、预算范围外的变更及平台要求按适用授权处理。受理未知优先对账和取回。

## 显式选用单项适配器

适配器任务写明 modality、正式来源和条目、完整提示词、参数、真实绑定、输出与覆盖权限。参考槽从 1 连续编号，每个路径只占一个槽，按用途编译供应商角色。音频另按实际发声角色绑定。普通补录从剧本取台词，音乐从视频提示词的音乐章节取已接受内容。

## 命令

格式与契约见 [adapter-contract.md](references/adapter-contract.md)，CLI 由 [production_tool.py](scripts/production_tool.py) 提供：

```text
python3 {技能目录}/scripts/production_tool.py prepare <project> --job <job.json>
python3 {技能目录}/scripts/production_tool.py confirm <project> --job-id <id> --confirmation "CONFIRM <id> <code>"
python3 {技能目录}/scripts/production_tool.py run <project> --job-id <id> --adapter-config <outside-project-config.json>
python3 {技能目录}/scripts/production_tool.py status <project> --job-id <id>
python3 {技能目录}/scripts/production_tool.py collect <project> --job-id <id> --adapter-config <outside-project-config.json>
python3 {技能目录}/scripts/production_tool.py audit <project>
```

同一 job 存在未决 `running` attempt 时禁止重新 `prepare`、`confirm` 或 `run`。

## Adapter 边界

adapter 配置保存在项目外，凭据只从运行环境或系统凭据存储读取；项目文件与 Dashboard 不保存密钥。输入输出协议与引用语义追加规则见 [adapter-contract.md](references/adapter-contract.md)。

可选内置 stdlib adapter（离线自检夹具 `fixture_adapter.py` 仅供测试）：

- [Seedance](references/providers/seedance.md)：显式配置模型/Endpoint ID，支持图片、视频和音频参考 role。
- [GPT Image 2](references/providers/gpt-image-2.md)：无参考图走 generation，有参考图走 edit，校验尺寸与透明背景限制。
- [MiniMax Music](references/providers/minimax-music.md)：固定 `music-3.0`，区分主题曲与纯配乐。
- [MiniMax 语音](references/providers/minimax-speech.md)：`tts` 模态，显式要求模型与预置 `voice_id`，不做声音克隆。
- [MiniMax H3 视频](references/providers/minimax-h3-video.md)：显式配置模型 ID、分辨率集合与时长区间，声画同次生成。

## 中断恢复与复核

异步任务拿到供应商任务 ID 后会在首次轮询前写入 `handle_path`。轮询或下载中断不等于未执行：先运行 `audit` 检查是否存在 `orphaned_provider_job`（`action: collect_before_retry`），再用 `collect` 取回原任务结果（不重新提交、不消费新确认）；确认原任务已终态失败且无法取回后，按 `retryable` 和当前任务授权登记恢复。

成功后回报实际输出路径、媒体类型与运行状态，不把技术成功或哈希一致写成媒体质量结论。若失败证据指向提示词、参考图或音频缺陷，将证据交回对应负责技能修订，更新正式来源和任务快照。动作与台词由各自 owner 修订。后续剪辑路由 `$short-drama-edit`，质量复核路由 `$short-drama-review`。

## 安装维护

只有安装、升级或排障时运行离线自检：

```bash
python3 {技能目录}/scripts/selftest.py
python3 {技能目录}/scripts/provider_adapters.py --selftest
```
