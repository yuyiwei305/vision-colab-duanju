---
name: short-drama
description: 基于文件系统初始化和继续短剧或漫剧项目，提供 creator-first 五文档路由、本地 Dashboard、制作形态与 Look Development 决策。用户提出“创建/继续短剧项目”“看进度/下一步”“做 Look Development”“打开 dashboard/短剧创作台”“导出制作资料”，或任务跨多个创作阶段时使用；明确的写作、资产、提示词、分镜、剪辑或审查请求由对应子 skill 直接处理。
license: MIT
---

# 短剧创作路由

本技能负责项目初始化、跨阶段路由、制作形态与 Dashboard。各阶段正文由对应技能完成。

**Vision Media** 依[项目技能入口](../../../../.agents/skills/vision-media-workflow/SKILL.md)路由。H3 V4 的镜头、参考和续接见[段落契约](../../../../docs/h3/shot-motion-contract.md)，生产与恢复走[工厂入口](../../../../docs/factory/README.md)。

## Quick Start

创作统一使用 [creator-first 工作流](references/creator-workflow.md)：每集按需维护 `剧本.md`、`视觉设定.md`、`分镜.md`、`图片提示词.md`、`视频提示词.md`，不建立并行的结构化创作真相。各文档格式见 [五份创作文档](references/creator-documents.md)。

## 路由

| 用户要做什么 | owner / 行为 |
|---|---|
| 开发点子、系列承诺、改编和分集地图 | `$short-drama-develop`，仅在用户需要时 |
| 已有多集完整剧本/散稿识别分集 | `$short-drama-develop` 按实际边界建立临时索引 |
| 分析长篇原著 | `$short-drama-novel-analyze`，仅在用户需要时 |
| 拆解对标剧、和标杆比差距、用自家成片对照 | `$short-drama-breakdown` → `项目开发/对标拆解.md` |
| 写或改单集剧本 | `$short-drama-write` → `剧本.md` |
| 拆人物、造型、地点、道具 | `$short-drama-assets` → `视觉设定.md` |
| 写资产图片提示词 | `$short-drama-image-prompts` → `图片提示词.md` |
| 做镜头和冻结关键帧 | `$short-drama-storyboard` → `分镜.md` |
| 写视频/时间线音乐提示词 | `$short-drama-video-prompts` → `视频提示词.md` |
| 实际生成媒体 | `$short-drama-produce`，预览并按任务授权登记后运行 |
| 把已生产的素材剪成成片 | `$short-drama-edit` → `剪辑单.md` 与 `制作成果/成片/` |
| 审稿、投产或回片审查 | `$short-drama-review`，按任务范围和阶段工单执行 |
| 初始化、Dashboard、归档点名文档 | 本技能 |

可从任一已有材料直接进入下游阶段，不为补齐名义流水线伪造上游。修改故事、镜头职责或上游时长声明时，回到负责该内容的文档。

## 执行请求

1. 定位用户给出的项目或资料，只读当前任务的直接输入。
2. 把用户点名的完整范围交给相应技能；批次只控制上下文，自动续跑。
3. 只有真实创作分叉才询问；不要拿 schema、目录、事务或检查器询问创作者。
4. 制作任务执行交付所需的阶段验收。仅写稿、做分镜或审查时止于各自范围。完成后回报真实产物、证据和未决项。
5. 请求横跨视觉设定、图片提示词、分镜或视频提示词时，结束前核对已有文档的视觉依赖；图片提示词和分镜并行完成时同步两支引用。每写完一份文档带 `--partial` 运行跨文档检查，五份齐备后运行完整检查，不为检查补造文件。
6. 参考缺失时先核实并复用项目实图，仍有缺口回对应图像负责人。提示词可带缺口交接，实际投产须绑定已验收实图。
7. 用户在会话里点名目标视频模型（如“按 MiniMax H3 写”“用 Seedance 2.5”）而 `short-drama.json` 的 `production_profile` 仍为 `unset` 时，先把该选择及其原生时长、参考方式和正文语言写进档案，再继续下游阶段。

## 初始化与项目级档案写入

需要项目配置时运行：

```bash
python3 {技能目录}/scripts/project_tool.py init ./my-drama --title "示例短剧"
```

首次 `init` 把输入中已确认的事实直接带上（`--language`、`--prompt-language`、`--aspect-ratio`、`--episode-count`、`--target-seconds`），只省略未确认项。`init` 只建配置和空目录，不预建空文件。项目定位与安全写入见 [运行预检](references/runtime-preflight.md)。

生产档案、语速、集数、画幅与节奏档案均通过“发布决策 → 接受 → `set-authority`”三步写入（状态只用 `accepted` 或 `unset`）：

```jsonl
{"decision_id":"CD-H3","status":"accepted","target_locators":[{"src":"short-drama","field":"/creator_authority/production_profile/choices"}],"accepted_value":{"target_video_model":"minimax-h3","video_prompt_dialect":"minimax-h3","video_prompt_language":"en","native_duration_seconds":{"min":4,"max":15},"supported_generation_modes":["reference"],"audio_generation":"same_pass"}}
```

```bash
python3 {技能目录}/scripts/project_tool.py publish <project> --owner short-drama \
  --artifact-id AR-PROFILE --output "创作者决策/production-profile.jsonl=输入/profile.jsonl"
python3 {技能目录}/scripts/project_tool.py accept <project> --artifact-id AR-PROFILE --decision accepted
python3 {技能目录}/scripts/project_tool.py set-authority <project> \
  --field /creator_authority/production_profile/choices \
  --decision-ref "创作者决策/production-profile.jsonl#CD-H3"
```

各字段路径与取值约定：

- **生产档案**（`--field /creator_authority/production_profile/choices`）：取值按 `$short-drama-video-prompts` 对应模型方言文件的推荐档案填写，写完用 `status` 复核 `video_model_profile`。
  - 其他模型明确采用其多镜容器时，在适用档案中登记 `"multi_shot_containers": true`。
  - Vision Media 的 H3 原生续接按分镜映射编译并使用真实 AV latent。
- **语速与集数**：`--field /format/pacing` 的 `accepted_value` 必填 `{"spoken_characters_per_second": <每秒字数>, "seconds_per_action_paragraph": <每段动作秒数>}`（英文台词用 `"spoken_words_per_second"`，中英混排则两项都给；画面文字按读字计时时可加 `"on_screen_text_characters_per_second"`；无实测值时动作段可从 2.5 秒起提，首集分镜后回校）。`--field /format/episode_count` 填正整数。`status` 的 `profile_notes` 若提示设定冲突或缺语速，立即交创作者决定。
- **画幅与制作形态**：画幅随播放面与创作者决定（`--field /format/aspect_ratio`，如 `"16:9"` 或 `"9:16"`），不因短剧默认竖屏；`init` 的默认值只是占位，未确认时连同播放面问一次。做过同播放面对标拆解时，把对标最常见的形态（如横屏三维国漫）列为候选，不只在二维画风间挑选。Look Development 是可选分支，不是进入图片提示词或分镜的固定门槛。
- **节奏档案**（`--field /creator_authority/rhythm_profile`）：字段与默认值由 `$short-drama-develop` 按制作形态提出，创作者确认后写入不含 `status` 的完整对象（已接受后单改某值时 `--field` 指向该子字段）。下游只核对 `status: "accepted"` 的档案；手改不合法时问题列在 `status` 的 `rhythm_profile_problems`。

## Dashboard

用户明确要求打开创作台时运行：

```bash
python3 {技能目录}/scripts/dashboard_server.py --workspace <workspace> --port 0 --detach --open
python3 {技能目录}/scripts/dashboard_server.py --workspace <workspace> --status   # 打印当前链接
python3 {技能目录}/scripts/dashboard_server.py --workspace <workspace> --stop     # 停止
```

`--detach` 使进程后台独立运行，地址、端口和 pid 记在 `<workspace>/.short-drama/dashboard.json`，日志在 `dashboard.log`；已有实例复用原链接，换端口或令牌加 `--restart`。结构视图只读，改动在「编辑原文」保存。镜头没有起始帧绑定时，起始帧缩略图可为空。源片与成片按剪辑单「来源」和剪辑输出定位。

## 按问题按需读取

- 规则分级与 owner 路由：[规则与路由索引](references/knowhow-index.md)
- 输出语言、稳定 ID、所有权与安全边界：[契约与所有权](references/contract-and-ownership.md)
- 实拍、二维、三维、水墨、Q 版、国漫的形态差异：[制作形态](references/production-form-profiles.md)
- 需要比较代表帧时：[Look Development](references/look-development.md)
- 参考图能控制什么：[参考角色](references/reference-roles.md)
- 遮挡、延迟揭示和观众知情时机：[观众揭示](references/audience-reveal.md)
- 母版、补拍和替代版的职责：[补拍与替代](references/pickup-and-alternate.md)

## 生产、导出与维护

生产保留预览、授权登记与运行回执，执行范围按任务和当前适用规则。用户要求导出当前资料时运行：

```bash
python3 {技能目录}/scripts/project_tool.py export <project> --out <项目外目录>
```

`export` 将每集现有五份创作文档、`剪辑单.md` 和 `剧集/<EP>/制作成果/` 复制到项目外的 `--out`（排除 `输入/`、`交付/` 和 `.short-drama/`；可选 `--episode EP001`、`--no-media`、`--overwrite`），生成 `manifest.json`（`asserts_approval` 恒为 `false`，代表当前状态快照而非审批结论）与 `checksums.sha256`。需要带审批证据的正式交付包走 `package` / `verify`。

仅在安装、升级或排障时运行 `python3 scripts/selftest.py`。
