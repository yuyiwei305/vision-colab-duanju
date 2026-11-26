# 短剧工厂操作手册

工厂管理阶段工单、H3 排产与恢复、剪辑交接及整片增强。

[段落契约](../h3/shot-motion-contract.md)：`episode plan` 输出段落、生成段、镜头的归属和区间，并分别计数；预算和并发按实际 MOTION 调度。剪辑 CUT 带实际素材区间和明确 SHOT，不把计划切点当成源入出点。

新集只增加正式正文、规格和账本数据，不再建立单集 Python、Shell 或 JS 运行脚本。缺少能力时修复共享模块并发布新版本。创作仍由当前 Codex 会话执行阶段技能；CLI 管理确定性的规格、依赖、任务、回执与恢复，不能代替剧情判断、看图或听审。

创作到交付的顺序与交接标准见 [18 步剧集制作总 SOP](../剧集制作总SOP.md)，本手册只说明系统操作，不另设生产流程或审批。

## 1. 入口与事实来源

```bash
bash scripts/drama --help
bash scripts/drama release verify
bash scripts/drama skills --check
bash scripts/drama skills --step 6
bash scripts/drama episode status '作品/作品名' --episode EP001
bash scripts/drama episode work '作品/作品名' --episode EP001 --stage write
```

先读项目入口和 `short-drama`，再消费 `episode work` 返回的阶段技能、引用资料、正式输入和检查要求。`work` 创建工单，不表示这些技能已执行。完成真实工作后，用 `episode complete --order <工单ID> --evidence <作品内文件> --observations <真实检查>` 登记；可重复传 `--evidence`。结构检查、执行者选用与用户确认分别记录。

| 信息 | 唯一来源 |
| --- | --- |
| 故事、视觉、分镜、图片和视频提示词 | 五份正式 Markdown |
| 剪辑取舍、字幕、声音和效果要求 | `剧集/EPnnn/剪辑单.md` |
| 作品默认值、本集冻结规格 | `short-drama.json`，本集位于 `creator_authority.episode_specs` |
| 人类可读的授权、保留项、续做决定 | 作品既有 `创作者决策/项目约定.md` |
| 计划、任务轮次、调用与素材回执 | 官方 `.short-drama/production/` 内的派生记录 |
| 工具真实执行结果 | 官方 job、provider handle、返回文件或工程回读 |

计划、工单和索引均可重建；不能编辑其中的提示词来替代正式正文。`episode status` 并列呈现阶段证据、问题、任务预算和恢复状态，文件存在不会被判为质量通过。

`skills --check` 校验 12 个随仓技能的锁定文件、自发现入口和 18 步必读资料。用 `skills --step N` 读取当前步骤正文。工单沿用同一份路由，`sop_step` 指向总 SOP。生成和剪辑所需的宿主工具在当前会话单独核对。

## 2. 本集规格与文档修订

```bash
bash scripts/drama episode spec '作品/作品名' --episode EP001
bash scripts/drama episode spec '作品/作品名' --episode EP001 \
  --override <本集例外.json> --freeze --authorization '<本次已授权要求>'
```

例外 JSON 只填已确定的差异，例如竖屏原生交付：

```json
{
  "production": {"native_canvas": [768, 1344]},
  "delivery": {"canvas": [768, 1344], "fps": 24, "fit_policy": "preserve", "editor": "chatcut"}
}
```

生产保持 H3 V4 的 1344×768 / 768×1344、24fps。交付画幅变化必须有明确的裁切或留边策略，不能悄悄裁成数学 16:9。冻结后的本集不再跟随作品默认值变化。历史集首次解析会核对其原始官方 job 画布；存在多种历史画布时要求明确选择，不猜测。

`document inspect --path <作品内正式文件>` 返回当前 SHA 和条目。多文件改动使用 `document revise --request <JSON>`，请求字段为：

```json
{
  "changes": {"剧集/EP001/分镜.md": "修改后的完整正文"},
  "expected": {"剧集/EP001/分镜.md": "inspect 返回的原 SHA256"},
  "reason": "本次任务内的具体修订依据"
}
```

系统保存前后全文，在作品写锁内核对预期版本；遇到并发修改拒绝覆盖。中断后使用 `document recover <作品>`，必要时指定 `--revision <ID> --rollback`。有活跃 GPU 的正式输入会被保护；审查草稿写在活动快照之外。

## 3. H3 统一排产与总预算

先完成分镜、提示词、实际图片验收、H3 三阶段专项记录和全部分片准备，再创建任务。以下额度均为占位符，必须来自当前任务，不能复制历史作品预算。

```bash
bash scripts/drama episode plan '作品/作品名' --episode EP001 --goal clips --max-instances 2
# 使用上一步返回的 plan 路径
bash scripts/drama task create '作品/作品名' --id ep001-main --plan <plan.json> \
  --authorization '<任务授权来源与范围>' --max-ccu <本次总上限> --max-seconds <本次总秒数>
bash scripts/drama task prepare '作品/作品名' --id ep001-main --plan <plan.json> --reason '首次生产'
bash scripts/drama task run '作品/作品名' --id ep001-main
```

`plan/create/prepare` 均不开 GPU。`prepare` 生成官方 jobs、核对编译结果并按任务授权登记快照；不是要求用户逐项回复。只有 `run/resume` 会执行已授权的远端工作。

- 整份视频提示词不再限制 32 段；每批最多 32 段，同一依赖组不能跨批拆开。
- AV latent 链和动态原生音色依赖按完整连接组分片。同组超过 32 时先在正式文档中调整已听核 WAV 的复用方式；不能拆断真实续接链。
- 同一分片的多个批次串行复用实例与模型；不同分片最多 2 台 G4。40 段以上的 4 台目标仍未开放，预览说明实际降档原因。
- 任务契约冻结授权、总预算、总期限及执行版本；补拍追加 wave，旧 wave 保留原字节，不另起额度。
- `--entry MOTION-...` 可重复传入，只选择本次范围；计划自动补齐必要依赖。已完成消费素材是否可复用，按正式来源及当前选用检查。
- `episode impact --before <旧计划> --after <新计划>` 输出直接变化及依赖影响，不能代替内容审查。

补拍时回 owner 修改正式文档、重做实际受影响的检查、重新 `plan`，然后向原任务 `task prepare --reason <修订原因>`。前轮尚有活动或未知请求时禁止追加重复尝试。未提交过的准备轮次允许安全替换，所有记录仍保留。

```bash
bash scripts/drama task status '作品/作品名' --id ep001-main
bash scripts/drama task reconcile '作品/作品名' --id ep001-main
bash scripts/drama task resume '作品/作品名' --id ep001-main
bash scripts/drama task stop '作品/作品名' --id ep001-main --reason '<实际停止原因>'
```

本地 `status` 不查询远端；`reconcile` 查原任务；`resume` 先取回已知结果再判断待执行范围。远端响应未知时保留 unknown，不能重新 POST。已完成文件丢失或有意删除不自动重生成。

从 V20 起，Colab 分配已进入释放流程但尚未核实时，`task resume` 可按该批的原分配记录完成对账与释放。这次恢复止于资源收尾。

如果实际扣费低于上限却停止提交，先核哪些请求已受理、哪些尚未提交，再核已释放波的结算差、活动租约费用上界、提交预留和硬停时间。用账本说明具体拦截条件，实扣与正在使用的上界分别记账。仍可继续时恢复原任务。

需要升级既有任务的执行包时，先对账、取回结果并核实该任务实例已释放。按授权运行 `task upgrade-execution <作品> --id <任务> --authorization <依据>`。每次登记一个受支持的升级版本，到账本核对实际版本后再追加新轮。原预算与截止时间继续生效，受理未知时继续对账。

分波送审时，可在首波 `task prepare` 加 `--retain-runtime`。本波文件取回后保留原实例。后波复核正式输入后用 `task prepare --reuse-runtime`，承接同一任务唯一保留实例。以前的已提交请求须取回。账本证明从未提交的请求可随旧波结束。执行仍核验实例在线，沿用原总预算与期限。执行版本兼容时可移交保留实例。

额度或期限的变更按新增授权用 `task extend` 登记，所有租约须先结束。原期限已过、创作者批准从恢复时起继续时，用 `--from-now --additional-seconds <获准的续跑秒数>`；原任务累计费用和总上限继续生效。原生生图工具费用未接入 CCU 账本。

单实例任务的已释放租约，在分配前和释放后都确认账户空闲且存在有效余额扣减时按该扣减结算。多实例任务的同一波须确认共同空闲基线、所有实例已释放、结算后账户空闲、余额记录连续且未与其他波重叠，才一次计入合计扣减。未结清部分仍按时长上界估算。新单实例任务，或旧租约已结束且当前波只有一个批次的追加任务，按剩余额度与费率上界设硬释放时间并保留取回和解分配时间。预算与截止时间继续累计。

<a id="colab-g4-allocation-503"></a>

### Colab G4 分配返回 HTTP 503

1. **先定位分配阶段。** 核对原任务的 CLI 日志、实例列表和 CCU 账单。分配预检 `GET /tun/m/assign` 返回 200 只表示预检成功，实例以分配回执和实际会话为准。记录分配 POST 的状态、耗时及脱敏错误摘要。
2. **保留原 `nbh`，有限等待。** H3、ReCaVSR 和 Breeze 共用 Colab 分配控制器，首次提交前把请求号和截止时间写入该批次的 `allocation-request.json`。每次退避后先查询同一 `nbh`，查到实例就接回，查无实例才重试。进程中断后仍使用同一笔记录。手工另开 `colab new` 会产生新的 `nbh`。[Colab CLI 分配恢复](https://github.com/googlecolab/google-colab-cli/blob/main/docs/01_session_management.md#http-timeout-and-allocation-recovery)
3. **把超时设在授权窗口内。** 工厂按当次授权、任务余额和剩余期限预留取回及释放时间，每次分配最多累计等待 30 分钟。`COLAB_ALLOC_TIMEOUT` 可缩短该窗口。工厂按窗口限制单次 HTTP 请求，502／503／504 或可重试超时按 5、10、20 秒退避，之后每次最多 20 秒。已知的拒绝直接进入对账。模型初始化和生成各用自己的超时。
4. **按对账结果续跑或停下。** 拿到实例后核实实例和模型已就绪，沿用冻结输入、绑定音色和原预算，只续跑确认未提交或可恢复的部分。初始化或等待回执超时，优先在可恢复的原实例核查初始化和生成状态。已出片先取回并逐段核验，再释放实例。分配期满仍未取得实例时停止分配，复查原 `nbh` 与实例列表，用 `task reconcile` 核原生成任务并记录任务、计费状态和剩余范围。


## 4. 原生生图与资产回绑

额外镜头图按[冲击帧规则](../h3/shot-motion-contract.md#冲击帧与-h3-构图参考)从分镜进入生图流程。供导演对照的冲击帧按[实图验收](../image-generation-sop.md#visual-pacing-image-review)登记。拟送 H3 时，确认是完全独立 Motion 的起镜，再按构图用途回绑。

执行顺序是 `asset call prepare → dispatch → 当前宿主调用 image_gen → result → register → 实际看图 → observe → select`。每一步都通过 `bash scripts/drama asset <命令> <作品> --episode EP001 ...` 执行，详细参数见各子命令 `--help`。

- `call` 使用正式 IMG 条目；dispatch 在外部调用前登记，result 记录真实返回文件和工具回执。文件须位于作品内；不得虚构 provider job。
- unknown 先查原调用。只有明确技术失败可有界 retry，最多三次；审核失败不换渠道重试。
- `register` 验 SHA、实际尺寸和完整图片解码；`observe` 保存真实看图方法、结果和问题。
- 纯通用资产可先凭 `视觉设定.md` 与正式 `IMG-*` 执行到 `observe`。整集 `image-prompts` 工单依赖 `分镜.md`。等实际 `REF-*` 消费槽建立后，再执行 `select` 回绑。
- `select --slot REF-... --authorization <依据>` 通过同一修订事务更新分镜与视频提示词的引用，保留旧版。
- 只调整明确的声音/时长字段不会无故作废无关画面观察；影响视觉或无法判断的变化仍需复核。

角色新音色默认按 [Breeze TTS 2 SOP](../Breeze-TTS-角色音色SOP.md) 走正式 `tts` job 和共享 Colab 控制器；先设计、取回释放、听核，再绑定普通 H3 参考音频。样音和 H3 回片分别实际听核。

## 5. 剪辑、字幕与工程回读

```bash
bash scripts/drama timeline prepare '作品/作品名' --episode EP001
bash scripts/drama timeline readback '作品/作品名' --episode EP001 \
  --settings <设置.json> --snapshot <完整快照.json> --details <完整元素详情.json> \
  --media <素材库.json> --scene-source <场景脚本.txt>
bash scripts/drama timeline diff '作品/作品名' --episode EP001 \
  --timeline <时间线.json> --readback <回读.json> --bindings <绑定.json>
```

`readback` 直接适配 OpenChatCut 返回数据。ChatCut 由宿主读取项目/轨道/字幕并整理为同一结构，再调用 actions；该路径已做适配单测，尚未进行真实工程往返验收。

时间线采用整数帧及有理帧率，源范围为 `[in, out)`。bindings 记录实际项目、轨道、素材 SHA→工具媒体 ID、CUT/字幕→元素 ID 和已采用样式。导入、绑定必须有实际工具结果，不能按文件名推测字节身份。

`timeline actions` 以 `--expected-readback-sha` 生成持久化原生工具包。宿主按以下协议执行：

1. 读取最新工程，核对差异，只选择仍需处理的动作；不盲目执行整包。
2. `timeline action --packet <ID> --action-id <ID> --state dispatch --readback <新回读>` 登记未知状态，然后调用指定原生工具。
3. 保存真实回执，以 `--state completed --receipt <文件>` 登记，更新返回的素材/元素绑定。
4. 每次写入后重新回读和编译下一包。插入后的 trim/retime 补丁、字幕 revision、关键帧、音量包络及 ripple 连带变化均按回执处理；可能相互覆盖的更新由宿主合并为编辑器支持的安全操作，不机械按旧包顺序写入。
5. 结果未知先查询元素与回执，禁止重复插入。工程里的手动拆字幕、样式与关键帧先核实，再决定是否写回正式剪辑单。

脚本为可直接表达的视频和字幕生成工具参数；复杂特效、音乐、自然语言混音要求保留为 `host_work`，由 `short-drama-edit` 调原生工具落实。工具包始终不自行宣称可以导出；必须完成差异检查、内容与连续视听后，由宿主保存、导出并记录原生导出回执。

`timeline verify --path <作品内成片> [--source-audio <原声源片>]` 核实际规格、完整解码及可选 PCM 原声对照。它不会把内容检查或用户确认改为通过。

## 6. 全片增强

`finish prepare` 固定使用 ComfyUI ReCaVSR，整片一次等比超分，按 [成品画质增强 SOP](../成品画质增强SOP.md)执行。默认 `--short-edge 2160 --fps 60`，成功时只取回带声增强成片。

```bash
bash scripts/drama finish prepare '作品/作品名' --episode EP001 --id ep001-finish \
  --source <作品内成片.mp4> --cuts <切点帧数组.json> \
  --cuts-evidence '<实际核对记录>' --authorization '<本次增强授权>' \
  --max-ccu <本次上限> --max-seconds <总秒数>
bash scripts/drama finish run --batch <上一步返回的batch.json>
bash scripts/drama finish status --batch <batch.json>
bash scripts/drama finish resume --batch <batch.json>
```

切点文件是原片中下一镜开始的整数帧数组，例如 `[120, 264]`。核实无硬切则为 `[]`。prepare 核源 SHA、画幅、帧数、色彩、磁盘和独立预算。切点仅供 RIFE 跳过跨切插值。支持 24/25/30/50/60 整数恒定帧率、无旋转方形像素的 8-bit SDR，目标与源同帧率时直接回封原声音轨。升帧使用 `.local/finishing/rife-4.25.zip`，也可用 `--rife-zip` 指定符合锁定 SHA 的同一归档。

已生成但下载中断时用 `resume` 取回原成片。补帧失败时取回可复用的超分母版，继续使用原任务预算及期限。已有经回执验证、同一源片的 ReCaVSR 无声母版，也可用 `--master <作品内母版.mp4>`，沿用 `--source` 的原声。释放仅针对本任务实例，成功后核分配列表。

## 7. 发布、迁移与能力边界

新任务绑定当前[执行清单](releases/factory-v21.json)和 SHA，使用本工作区的 `scripts/drama` 通道完成排产与准备。H3 参考素材容量由[运行清单](../h3/workflow-v4-g4-runtime.json)统一定义。5–9 张参考图在 G4 上的显存与耗时仍待实测。

开始 H3 或增强任务前运行 `bash scripts/drama release verify`。新 H3 任务按当前专项技能指纹记录实际检查。已有任务继续用 `scripts/drama` 续做，工具按账本校验并调用任务锁定的执行包。 续做时需保留旧清单和对应的 `.local/factory-releases/` 快照。需要在原任务追加使用新版本的轮次时，按授权使用第 3 节的 `task upgrade-execution`，保留原任务与批次快照。
