# Vision Media 短剧制作工厂

用 Codex 这类能读写工作区、运行终端命令的 **Agent** 制作短剧和漫剧。Agent 负责剧本、分镜、提示词、看图和看片。仓库提供 18 步制作 SOP、阶段技能和工厂 CLI，管理规格、参考素材、Colab 排产、费用、取回、恢复与交付。

默认视频路线是 **MiniMax H3 V4 Ref2VA on Google Colab**。从剧本或分镜起步，只需本地环境。首个视频任务再准备 Google 账号、参考素材和授权预算。

## 把这段 Prompt 发给 Codex

在 Codex 打开仓库目录，粘贴下面整段。还没克隆也可以先打开一个用来放项目的目录，让 Agent 克隆。

```text
帮我把 Vision Media 短剧制作工厂准备好，默认目标是能用 Colab 跑 H3 视频。
如果我注明「只做文稿」，只准备本地基础环境。

1. 从当前工作区定位仓库。如果还没有，就把
   https://github.com/yuyiwei305/vision-colab-duanju.git
   克隆到当前目录下。进入仓库根目录，读取 AGENTS.md 和 README 的「环境准备」。
2. 直接执行适合本机的安装和检查。先复用已有工具，再补齐缺项。如果缺 Git，
   先按本机系统的步骤安装它，再克隆。用仓库的 .venv 隔离
   Python 和 google-colab-cli。视频环境验证锁定版本、ffmpeg、ffprobe；本地烧录
   中文字幕时再补 Fontconfig、中文字形和 FFmpeg 的 subtitles 滤镜。
   跑 README 的本地验收命令，再从仓库以外的临时路径初始化一个最小作品并核对工单。
3. 检查当前会话是否具备 image_gen 和 ChatCut，分别记录缺失对哪些步骤有影响。
   视频模式下先用 README 的工厂账号检查验证现有 Colab 登录。
   需要 Google 授权时，把准确的终端命令给我，请我在本机浏览器和终端完成。
   授权后继续验证。授权码只填在终端，凭据交系统钥匙串。
4. 本轮只准备环境，验证账号走只读查询。完成后用短清单报 Python、Colab CLI、
   工厂校验、媒体工具、登录和插件状态，并给出首个可运行的制作步骤。
   GPU 分配、模型部署和正式生成放到我另行确认的制作任务。
```

## 环境准备

### 1. 选择做到哪一步

| 当前目标 | 本机准备 | 账号与宿主工具 |
| --- | --- | --- |
| 剧本、分镜、提示词、离线排产 | macOS 或 Linux、Git、Bash、Python 3.12+。以下命令用 `uv` 管理 Python | 一个能读文件和跑命令的 Agent |
| 参考图生成与回绑 | 本地基础环境、FFmpeg | 会话能调用 `image_gen`，或有经用户选用、已验收的现成参考图 |
| H3 视频、Breeze 角色音色 | 本地基础环境、FFmpeg／FFprobe、Colab CLI 0.8.0 | Google Colab OAuth2、可用算力及本次任务预算。H3 使用 G4，新音色 Breeze TTS 2 使用 L4 |
| 剪辑、字幕与交付 | FFmpeg／FFprobe。本地烧录中文字幕需要 Fontconfig、可用中文字体、带 libass 的 FFmpeg | 默认用当前会话的 ChatCut 插件和可编辑工程 |
| 全片超分与补帧 | 视频环境。补帧另外准备原版 RIFE 4.25 ZIP | ReCaVSR 使用 Colab G4。按需要安排独立增强任务 |

Windows 用户可在 WSL2 的 Ubuntu 环境中按 Linux 路线准备。先确认 Agent 的命令也在 WSL2 内执行，并验证该会话能访问 OAuth2 所需的钥匙串服务。Colab CLI 官方支持 macOS 和 Linux。[Colab CLI 0.8.0](https://pypi.org/project/google-colab-cli/0.8.0/)

### 2. 补齐本机工具

**macOS** 缺 Git 时可用 `xcode-select --install` 安装 Apple 命令行工具。系统有 Homebrew 时直接用 `brew`。首次安装按 Homebrew 官网执行，并根据安装器提示让当前终端载入 `brew shellenv`。FFmpeg 按用途选一个版本。[Homebrew 安装说明](https://docs.brew.sh/Installation)

```bash
brew install git uv
# 生成、取回和验证媒体
brew install ffmpeg
# 本地烧录中文字幕用这组替代上一条
brew install ffmpeg-full fontconfig
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"
# 仅在系统没有可用的简体中文字体时安装
brew install --cask font-noto-sans-cjk-sc
```

Homebrew 的 `ffmpeg-full` 带 libass，且安装后需要将它的 `bin` 放到当前终端的 PATH 前面。系统已有带 `subtitles` 滤镜的 FFmpeg 时直接用实测可用的版本。[FFmpeg Full](https://formulae.brew.sh/formula/ffmpeg-full) · [Noto Sans CJK SC](https://formulae.brew.sh/cask/font-noto-sans-cjk-sc)

**Ubuntu／Debian** 使用 apt。第三条命令为媒体工具，按目标选择。其他 Linux 发行版用对应包管理器安装同等工具。

```bash
sudo apt-get update
sudo apt-get install -y git bash curl ca-certificates
sudo apt-get install -y ffmpeg fontconfig fonts-noto-cjk
```

**缺少 uv** 时，使用官方安装器。以下命令让当前终端找到安装后的 `uv`。[uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

### 3. 克隆并建立项目环境

```bash
git clone https://github.com/yuyiwei305/vision-colab-duanju.git
cd vision-colab-duanju
if [ ! -e .venv ]; then uv venv --python 3.12 .venv; fi
source .venv/bin/activate
python3 --version
```

`uv` 会按需获取 Python 3.12。已有 `.venv` 时先验证其 Python 为 3.12 或更新版本。工厂的本地命令用 Python 标准库，创作阶段技能随仓库提供。每次开启一个新终端，先进入仓库并执行 `source .venv/bin/activate`。[uv 的 Python 环境说明](https://docs.astral.sh/uv/pip/environments/)

### 4. 准备 Colab 视频环境

用同一个仓库环境安装固定版。工厂会解析 `colab` 启动器，使用同目录的 Python 调用 Colab CLI，所以启动器和 Python 必须来自同一个环境。

```bash
uv pip install --python .venv/bin/python 'google-colab-cli==0.8.0'
uv pip check --python .venv/bin/python
.venv/bin/colab version
```

版本应为 **0.8.0**。激活后 `colab` 从 `.venv/bin` 取得。Agent 有独立 shell 或机器上装了多个 `colab` 时，在运行工厂命令的同一个 shell 显式指定 `export COLAB_CLI="$PWD/.venv/bin/colab"`。仓库依赖该版本的内部接口，版本升级先做兼容性验证。[固定版本发行包](https://pypi.org/project/google-colab-cli/0.8.0/)

**第一次登录时，在本机交互式终端运行**下面的只读命令。打开终端输出的 Google 授权链接，选择用于 Colab 的账号，把 Google 返回的授权码填回原终端。CLI 将凭据存入系统钥匙串。

```bash
.venv/bin/colab --auth=oauth2 usage
```

**验证同一登录可被工厂继承**。以下检查查询账户使用状态，成功会输出确认信息。Agent 可先执行此检查，有效登录可直接复用。

```bash
export COLAB_CLI="$PWD/.venv/bin/colab"
python3 -B - <<'PY'
import sys
sys.path.insert(0, "scripts")
from colab_h3 import Colab
Colab("setup-check").control("usage")
print("Colab OAuth2 可用，账户查询通过")
PY
```

若每次都要求授权，先检查钥匙串能否保存并在 **Agent 所用的同一系统用户和会话**中读取。macOS 用登录钥匙串。Linux 可用已解锁的 Secret Service，例如桌面会话中的 GNOME Keyring。无钥匙串的 SSH、容器或 WSL2 会话需要先配好凭据存储。账户查询通过后，G4／L4 实际分配仍取决于账户资格、余额和当时资源。[Colab OAuth2 与钥匙串说明](https://github.com/googlecolab/google-colab-cli/blob/v0.8.0/docs/04_automation_and_utility.md#authentication-strategies-cli-backend)

### 5. 验收本地环境

在已激活 `.venv` 的仓库根目录执行。前两条分别校验内置技能与执行包。最后两条在临时目录跑一遍项目初始化和写作工单，无需账号或 GPU。

```bash
bash scripts/drama skills --check
bash scripts/drama release verify
smoke_dir=$(mktemp -d)
bash scripts/drama project init "$smoke_dir/试跑" --title '试跑' --language zh-CN --aspect-ratio 9:16
bash scripts/drama episode work "$smoke_dir/试跑" --episode EP001 --stage write
```

视频环境另外验证 FFmpeg 和 FFprobe。本地烧录中文字幕时检查滤镜和字形，`fc-list` 应能列出至少一种中文字体。

```bash
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -filters | awk '$2 == "subtitles" { found = 1 } END { exit !found }'
fc-list ':lang=zh-cn' family
```

## 开始第一集

准备三样输入即可开工。

1. **故事材料**。一句灵感、大纲、完整剧本或旧工程都可以。已有作品直接提供作品目录和目标集，Agent 核对交接后接续。
2. **成片要求**。给画幅、预计时长、语言和本次做到哪一步。有必须保留的对白、已有角色图、配音或参考视频时，把来源一并交代。
3. **媒体生成权限**。实际出片时给本次范围和累计算力上限（CCU）。如果有交付时间或指定停点，一起写明。预算未确定时，可以先交分镜、提示词和排产预览。

新作品可把下面的任务交给 Agent。

```text
按 AGENTS.md 和 18 步 SOP，用我给的故事材料做《作品名》EP001。
语言中文，竖屏 9:16，目标约 60 秒。本轮做到剧本、视觉设定和分镜，
写入作品目录。先核我给的原稿和已有资料，每个阶段实际读取技能。
交付时说明本集的叙事转折、关键镜头和仍需验证的地方。
```

Agent 按阶段读文档、做创作判断，并用 `bash scripts/drama` 验证和管理状态。手动定位时，写作看 `skills --step 4`，分镜看 `skills --step 6`。作品目录在 `作品/<作品>/`，含各集正式 Markdown、媒体与本地运行账本，请自行备份。[18 步总 SOP](docs/剧集制作总SOP.md) · [工厂手册](docs/factory/README.md) · [剧本与分镜方法对照](docs/manju-laoli-comparison.md)

### 从分镜到成片

已有分镜和素材、想实际出片时，可填写下面的任务范围交给 Agent。

```text
续做「作品/作品名」EP001，用 H3 V4 生成 [指定段落或镜头]。
本次累计算力上限为 [CCU]。
按工厂 SOP 核对参考实图、声画时长和续接，在预算内冻结并执行本批。
取回校验后释放并复查本任务实例。按分镜顺序带原声连看前后镜，
交付可用版本、剪入剪出时间和实际消耗。
```

| 阶段 | 开始前核对 | 操作入口 |
| --- | --- | --- |
| 生图及音色 | 角色、景物、道具与实图职责。新角色音色先设计，实际听核后绑入视频参考 | [生图 SOP](docs/image-generation-sop.md) · [Breeze 音色 SOP](docs/Breeze-TTS-角色音色SOP.md) |
| 视频投产 | 核实实图、段落、声画时长、参考槽、生成预算。工厂按锁定清单在 Colab 部署 ComfyUI、节点与模型权重，本机只管理输入、进度和回片 | [H3 工作流](docs/h3/forest-laoli-workflow.md) · [段落契约](docs/h3/shot-motion-contract.md) · [排产与恢复](docs/factory/README.md#3-h3-统一排产与总预算) |
| 剪辑与交付 | 打开 ChatCut 工程，确认需要处理的轨道、源片版本和字幕。交付验实际导出 | [剪辑 SOP](创作者决策/剪辑SOP.md) |
| 按需增强 | 锁定原片、目标帧率与预算。RIFE 补帧用上游 4.25 原版 ZIP，放入 `.local/finishing/rife-4.25.zip` 或通过 `--rife-zip` 指定 | [成品画质增强 SOP](docs/成品画质增强SOP.md) |

制作分镜时，`project read-through` 可以按成片顺序先读画面，再恢复声音。补拍时，`project review-motion` 可以并排查看各版原片，并按指定区间连播相邻段。工厂按累计 CCU 管理新任务，每 10 分钟报告实际阶段与已取回段数。[片段审看命令](docs/factory/README.md#motion-review) · [排队与预算](docs/factory/README.md#3-h3-统一排产与总预算)

默认 H3 Colab 路线用上文的 OAuth2 登录，无需另配 MiniMax API Key。可选第三方 API 通道按适配器契约单独配置，通过 `DRAMA_ADAPTER_CONFIG` 指向仓库外的配置文件。[适配器契约](ref/drama-skills/skills/short-drama-produce/references/adapter-contract.md)

## 常见卡点

| 症状 | 怎么继续 |
| --- | --- |
| 终端有 Python，工厂却提示版本错误 | 在运行命令的同一个 shell 激活 `.venv`，核 `command -v python3` 和 `python3 --version`。 |
| 找不到 Colab CLI，或出现 `colab_python_required` | 核 `.venv/bin/python`、`.venv/bin/colab` 以及 CLI 版本。用绝对路径设置 `COLAB_CLI`，确保指向项目环境的启动器。 |
| `colab_command_failed` 或 OAuth2 反复登录 | 在本机交互式终端重跑 `.venv/bin/colab --auth=oauth2 usage`，处理授权或钥匙串提示，再跑工厂账号检查。 |
| 登入成功但 GPU 分配失败 | 先查原请求、实例和账单，再按原任务恢复。分配重试按[工厂手册](docs/factory/README.md#colab-g4-allocation-503)接续。 |
| 本地字幕报缺滤镜，或汉字显示成方块 | 用当前 shell 检查 FFmpeg 的 `subtitles` 滤镜及 `fc-list ':lang=zh-cn' family`。macOS 上优先用带 libass 的 `ffmpeg-full`。 |
| 会话里找不到 `image_gen` 或 ChatCut | 继续完成文稿、提示词或排产预览。生图可接已有合格图，剪辑阶段在具备对应工具的会话中接续。 |

## 许可与来源

项目代码和文档使用 [MIT 许可](LICENSE)。Drama Skills 固定为 [zenstory-ai/drama-skills v0.11.2](https://github.com/zenstory-ai/drama-skills/releases/tag/v0.11.2)，保留 [MIT 许可](ref/drama-skills/LICENSE) 及 [项目版本锁](drama-skills.lock.json)。H3 导演技能保留 [原 MIT 许可](.agents/skills/forest-laoli-h3/LICENSE)。模型及外部服务各自适用其条款，例如 [MiniMax H3 Community License](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE)。
