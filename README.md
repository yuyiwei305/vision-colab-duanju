# Vision Media 短剧制作工厂

用 Agent 制作短剧和漫剧。18 步覆盖收本、立项、剧本、视觉、分镜、参考生图、视频生成、剪辑、验收和交付。Agent 读取阶段技能并执行创作与视听审查，CLI 管理规格、生产计划、预算、回执和恢复。

## 从干净克隆开始

在 macOS 或 Linux 使用 Bash 和 Python 3.12 或更新版本。从仓库根目录运行：

```bash
bash scripts/drama skills --check
bash scripts/drama release verify
bash scripts/drama project init '作品/第一部短剧' --title '第一部短剧' --language zh-CN --aspect-ratio 9:16
bash scripts/drama skills --step 1
bash scripts/drama episode work '作品/第一部短剧' --episode EP001 --stage write
```

在 Agent 中打开仓库，从 [AGENTS.md](AGENTS.md) 开始，按 [18 步总 SOP](docs/剧集制作总SOP.md) 接收原稿或续做现有材料。每进入一步，运行 `bash scripts/drama skills --step N` 读取技能正文。作品和运行账本保存在 `作品/<作品>/`，默认由使用者自行备份。所需命令、恢复和交付流程见 [工厂手册](docs/factory/README.md)。

## 媒体工具

| 环节 | 使用条件 |
| --- | --- |
| 剧本、分镜、提示词和排产 | 仓库内置 12 个 Drama Skills 及项目专项技能，可离线核对和编排。 |
| 图片参考 | 当前 Agent 会话需能调用 Codex `image_gen`。用 `skills --step 7` 进入 [生图流程](docs/image-generation-sop.md)。 |
| H3 视频、Breeze 音色、ReCaVSR 增强 | 安装 [Google Colab CLI](https://github.com/googlecolab/google-colab-cli) 到独立 Python 环境，例如 `uv tool install google-colab-cli==0.8.0`，完成 CLI 登录并确认账户能分配所需 GPU。工厂从 PATH 查找 `colab`，也接受 `COLAB_CLI` 指定入口。执行前分别核对 [H3](docs/h3/forest-laoli-workflow.md)、[音色](docs/Breeze-TTS-角色音色SOP.md) 和 [增强](docs/成品画质增强SOP.md) 的资源、模型和输入。 |
| 剪辑和交付 | 默认 ChatCut，使用时需要当前会话的 ChatCut 插件和可编辑工程。媒体技术检查及适用的本地导出使用 FFmpeg 和 FFprobe。中文字幕本地渲染另需 Fontconfig 与可用中文字体。见 [剪辑 SOP](创作者决策/剪辑SOP.md)。 |

可选 API 通道使用项目外配置 `DRAMA_ADAPTER_CONFIG`，按 [适配器契约](ref/drama-skills/skills/short-drama-produce/references/adapter-contract.md) 设置。真实生成依赖宿主、模型、凭据与可用资源，按本次任务范围和预算执行。

## 许可与来源

项目代码和文档使用 [MIT 许可](LICENSE)。Drama Skills 固定为 [zenstory-ai/drama-skills v0.11.2](https://github.com/zenstory-ai/drama-skills/releases/tag/v0.11.2)，保留 [MIT 许可](ref/drama-skills/LICENSE) 及 [项目版本锁](drama-skills.lock.json)。H3 导演技能保留 [原 MIT 许可](.agents/skills/forest-laoli-h3/LICENSE)。模型及外部服务各自适用其条款，例如 [MiniMax H3 Community License](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE)。
