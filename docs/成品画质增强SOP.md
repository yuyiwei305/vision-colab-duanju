# 成品画质增强 SOP

新增强任务使用共享工厂 `bash scripts/drama finish`，默认 **ComfyUI ReCaVSR 整片等比超分至 4K → RIFE 补至 60 fps → 回封原声**。横屏默认以高 2160 像素为目标，16:9 为 3840×2160，7:4 为 3780×2160。竖屏以宽 2160 像素为目标。创作者指定的尺寸与帧率优先。任务成功后只取回带声增强成片，本地原片直接用于验收。

## 模型配置

- ComfyUI v0.45.0，搭配 [ReCaVSR 2.0.0](https://github.com/PedroMarinhoDev/ComfyUI-ReCaVSR/tree/831769467c89b35ee47926189ac8189be63883d1) 的流式视频节点。INT8 ConvRot、FlashDecoder、fast kernels、seed 42、`color_fix=none`，H.264 NVENC 写出原帧率母版。后接 RIFE 4.25。 需要升帧时，从上游 Trained Model 列表选择 2024-09-19 发布的 4.25 归档。保留 ZIP 原始字节，放到 `.local/finishing/rife-4.25.zip` 或通过 `--rife-zip` 指定原文件。工厂按[运行清单](finishing/recavsr-runtime.json)校验 SHA-256。 [Practical-RIFE 权重列表](https://github.com/hzwer/Practical-RIFE/blob/bbfd2ea90910789a860ea3e2b32a240cd577b75e/README.md#trained-model)

## 执行与检查

1. 锁定指定原片，核 SHA、实际画幅、帧时钟、音轨、色彩、显存、存储和独立预算。预览同一快照里的目标尺寸、目标 FPS、母版和交付成片路径。以探测结果判断原片规格，文件名仅用于定位。识别源片切点供 RIFE 与验收使用。
2. 每部原片通过 `Load Video → ReCaVSR Upscale Video → Save Video` 完整流式处理，一部片提交一次。直接等比重建至交付尺寸，输出一个全长原帧率母版。保持完整视频输入与输出，图中禁用 `Video Slice`，交付流程不拼接分段。ReCaVSR 自身逐块读取和编码，在一次连续任务内完成。源片的硬切可能使模型跨镜带入短暂时序残影，抽查切后连续画面。 [ReCaVSR 节点说明](https://github.com/PedroMarinhoDev/ComfyUI-ReCaVSR/blob/831769467c89b35ee47926189ac8189be63883d1/README.md#long-and-large-videos)
3. 在远端完整母版上用 RIFE 按目标时钟补帧。同帧率直接回封。直接切镜的两帧之间沿用该镜末帧，直到下一镜起始。目标帧数按源视频时长取最接近的目标帧数，半帧向上取整。原声音轨逐条直拷回封。
4. 只取回带声增强成片，核 SHA、完整解码、帧率、帧数、画幅、原声音频包和起点。核切点、人物、快速运动、画内文字和切后闪烁。完成取回及核验后及时释放本任务 GPU 并复查实例状态。文件及官方回执进入作品账本，连续视听单独记录。
5. 首次取回失败先恢复原任务下载并核 SHA。若已完成超分而补帧失败，先查同版完整母版能否复用。验收表列实际交付 FPS，24 fps 母版与约定的 60 fps 交付分别记录。复用字幕前按[剪辑 SOP](../创作者决策/剪辑SOP.md#3-字幕原声与时间线)核对应版本与时钟。

每部片登记一份任务快照。命令、规格与恢复见[工厂手册](factory/README.md#6-全片增强)。既有任务按原执行快照恢复。
