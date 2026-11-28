# Breeze TTS 2 角色音色 SOP

本 SOP 规定 H3 的角色音色设计、样音验收和参考音频绑定。

**角色理解与声音设计 → Colab L4 自托管 Breeze TTS 2 VoiceDesign → WAV 取回校验并释放 → 样音听核 → H3 参考音色 → 回片听核。**

本流程生成角色身份样音；H3 仍随视频生成对白、表演和场景声音。样音不作为每句对白的配音，不硬锁原句、节奏或口型。仅改 SOP 不启动 GPU、不生成音频、不改变既有素材的验收状态。

## 1. 先定人物声音，再准备样音

- 走 `vision-media-workflow → short-drama → short-drama-assets → short-drama-produce`。先查本作品同角色已听核、已采用的 WAV，跨集继续复用；不为新台词重新抽一个音色，不跨作品自动共享，也不把历史样音重标为 Breeze 来源。
- 新角色或明确要求重新设计的声音，先在正式 `视觉设定.md` 的人物条目写 `声音方向`：听感年龄、音区与质感、共鸣、口音语域、3–5 条可听判据、最易混角色的区分，以及专名读音。不要只按性别或“低沉有磁性”批量分配。
- 同条记录 Breeze `voice_design instruction`、短样句原文和固定角色 seed。样句可以来自剧本，也可以是明确标为设计素材的试音句；中文样句配中文 instruction。强烈哭喊、背景音乐、混响和旁人说话不放入身份样音。
- 主要角色及跨段出现的有声角色先锁定听核过的身份音色；场景情绪用语气、重音和气息表现。一次性短台词角色经创作者授权可用 H3 同期声，在视觉设定注明来源，并在回片核实际说话者和整句声音。完全无对白的段不绑声音身份。

## 2. 冻结输入与本次预算

每角色一个正式 `tts` job：`source` 指向真实 `视觉设定.md`，`source_entry` 对应人物标题，`prompt` 为样句，`parameters.instruction` 为设计描述，`parameters.seed` 为角色 seed。通过 `bash scripts/drama produce prepare` 建准确预览；确认登记按任务授权执行，用户指定的确认点优先。

批次沿用共享 [控制器](../scripts/breeze_tts_colab.py) 与 [远端 worker](../scripts/breeze_tts_colab_remote.py)。批次记录本作品的唯一 batch/session、job 列表、项目外 adapter 配置、正式输出和 `resource_state`，以及 `model`、`revision`、`repo_commit`、实际 `worker_sha256`、授权和 CCU 上限。adapter 调用该控制器的 `adapter --batch`。凭据不写入项目。

已采用的版本与实参：

| 项目 | 默认基线 |
| --- | --- |
| 模型 | `BreezeBlue/Breeze-TTS-2` |
| 模型 revision | `3e28c5151381a722f1d8661b4118c298caa77aa4` |
| 推理代码 | `breezeblue-ai/breeze-tts`，commit `a43d19e718b322e1cb3b2aa66f6d37d7945f4b21` |
| 资源 | 单台 Colab L4，一次载模、逐角色生成 |
| 推理 | BF16，`attn_implementation='eager'`；不是 SDPA 或已验证的 fast-all 路线 |
| 生成 | CFG 4.0，`repetition_penalty=1.1`；固定代码的 generation config：temperature 0.9、top_p 1.0、top_k 50、max_new_tokens 750，depth decoder 同为采样 0.9／1.0／50 |
| 环境 | 现有 worker 安装 `transformers==4.57.3`、`qwen-tts==0.1.1`。后者用于模型自带音频 tokenizer |
| 引用输出 | 24 kHz、单声道、PCM16 WAV；当前实现要求 2–15 秒 |

模型、代码和关键推理参数升级须按任务范围重新核验；未锁定所有传递依赖，不承诺位级重现。每批保存实际环境、版本和回执。

GPU 开机前完成角色设计、job、批次、准确预览与独立 TTS 预算。预算包括部署、载模、生成、恢复、取回和释放；不套用 H3 的 G4 数量档位或复制历史额度。当前控制器要求账户空闲，遇到其他任务实例运行时等待或报告，不能释放别人的实例。

## 3. 单批执行、取回与释放

```bash
# 本次 job 已经 produce prepare、批次和 adapter 已准备后才执行。
python3 scripts/breeze_tts_colab.py run --batch '<本次作品内 batch.json>'
```

- 默认自托管，不先试官方 demo／ZeroGPU。演示端点仅为明确另选的渠道，不把免费配额和历史报价当成稳定生产能力。
- 同一 L4、同一模型缓存依次生成本批角色。样音完成先取回、验 SHA 和大小、保存回执与正式输出；全部目标正常取回后结束批次并释放，不让 GPU 等待听审。
- 核 24 kHz／单声道／PCM16、2–15 秒、有限信号、非静音和削波情况；ASR 只辅助发现遗漏、重复或专名问题，F0 只辅助区分，均不能替代听音。
- 保存同字节引用到本作品 `输入/声音参考/VOICE-<角色>-breeze-vN.wav`，元数据保留样句、instruction、seed、模型/revision、代码 commit、参数、SHA、job 与真实听核结论。修订加版本，不覆盖已绑定的 WAV。
- 控制器保存请求 handle；连接或下载结果不明先用正式 `produce status/audit/collect` 查原 job，不重投。当前 Breeze 控制器只有 `run/adapter/guard/release`，没有 `prepare` 或 `resume`；不要把再次 `run` 当恢复。
- 当前控制器退出时在 `finally` 尝试 collect 后释放，须核对本地结果和失败回执。远端已释放且无本地文件时如实报告，不能声称仍可取回；只对已确认失败或丢失的缺项按剩余预算另行准备，不重做成功角色。
- 核实本任务 endpoint 已解除分配；账户本来空闲时 count/rate 应回到 0。手工收尾只用原批次的 `release --batch`，不操作其他任务。

## 4. 听核后绑定 H3

样音取回并释放后，汇总角色听审：身份与区分度、专名读音、完整字音、重复、串声、底噪和混响。用户明确指定听审确认点则等该项确认；执行者具备听音条件时可按任务范围核验。无法听音时一次汇总听审包交用户或指定审片人，不逐镜新设确认点，不把文件技术通过写成听审通过。

- `视觉设定.md` 的 `声音参考` 登记真实路径、用途和准入状态。默认仅借音色质感、共鸣、听感年龄；原样句、情绪、语速、录音空间和背景声不得跟入。未听核保持 `unverified`，用户听审的原话和采用范围单独记录。
- `视频提示词.md` 的普通 `参考音频` 槽照抄同一路径，`reference_audio` 对应真实角色，按本镜实际顺序分配 `<Audio N>`，最多 3 槽，保留关系为 `reference`。不绑定不存在的 WAV，不再默认填写 `取样规划`／`原生音色参考`。
- H3 正文独立控制逐字台词、说话人、起声点、语气、情绪、语速、场景声音和口型；真实 AV latent 续接仍按原规则，音色 WAV 不能替代续接。
- H3 开机前完成本轮样音听核、绑定和正式输入检查；取回后再听核实际对白、跨镜音色、字音及接缝。采用本 SOP 不代表未来每条样音或最终 H3 声音自动验收通过。

## 5. 许可

- 官方固定 commit 的 [README](https://github.com/breezeblue-ai/breeze-tts/blob/a43d19e718b322e1cb3b2aa66f6d37d7945f4b21/README.md) 区分代码 Apache 2.0 与模型权重、衍生模型及自托管输出的研究／非商业用途限制。默认 SOP 不构成商用许可；商业用途应先取得适用授权，或明确另选满足用途的渠道。不得假定自托管等同购买官方平台的商用权益。
