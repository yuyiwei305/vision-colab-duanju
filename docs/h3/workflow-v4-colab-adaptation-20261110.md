# H3 V4 执行配置

所有生产按[工厂手册](../factory/README.md#3-h3-统一排产与总预算)与[专项流程](forest-laoli-workflow.md)执行。提交前核推理档案和实际选档。

## 自动选档

唯一配置实现：[h3_workflow_v4.py](../../scripts/h3_workflow_v4.py)。它从正式`视频提示词.md`的当前镜头可复制正文读取`summary`和`detailed_description`，在开GPU前做本地规则判定，无外部模型调用：

- 执行中的打斗、劈砍、格挡、拳脚、追逐/冲刺等中英文动作词选打斗；其余选文戏。
- 人物身份设定里带兵器、声音说明、双引号内对白、明确否定句，不单独触发打斗。
- 可在镜头元数据增加`- H3档位：文戏`、`打斗`或`自动`处理歧义；不认识的值直接报错。
- 同一AV latent续接链使用同一档；链内存在打斗则整链选打斗。明确文戏覆盖与链内打斗冲突时，prepare前报错，不能在链中途静默换LoRA。
- 判定结果、依据、规则版本和链头写入job的`parameters.scene_routing`，随正式来源/参考指纹冻结，并完整出现在生产预览。运行中不重新分类。

这是可检查的动作规则，**不是通用剧情语义理解器**；隐喻、对白未用引号、冷门动作词可能误判。需在GPU前检查预览或使用上述正式字段覆盖；不外发作品给Jev或其他分类服务。

| 配置 | 文戏 | 打斗 |
| --- | --- | --- |
| 共用LoRA | Lightx2v 0.7 | Lightx2v 0.7 |
| 风格/动作LoRA | Realism People 0.8、电影质感0.3 | Combat V2 0.8、FrameRush V2 0.5、速度滑杆5 |
| 基础步数 / 低分前段 | 8 / 6 | 12 / 8 |
| Sigma Refiner | 两档均额外1步，0.7→0，cosine | 同左 |
| 预期采样NFE | 9 | 13 |
| 两组关系 | 互斥 | 互斥 |

底模为原图已连接的Cobijada Hybrid INT8，Qwen3-VL INT8编码器、原图两只VAE、V2 latent upscaler不变。DMAD和两个未连接备用底模不下载、不叠加。角色图片/音频仍由正式Markdown绑定进入Easy MediaBridge，图片与Audio分别编号；文戏编译时按原LoRA要求补`r34l1sm`，该处理属于固定编译契约，不改写正式剧情正文。

使用原生横竖画布。时长按 Easy 原生最近 `17k+5` 网格计算，画面为 24fps、H264/yuv420p/CRF12 并保留原声。取回与文件保留见[原生输出契约](native-output-and-retention.md)。

## 省显存路径

- 无LowVRAM Attention、FFN分块或Goohai二采分块节点；Self-Lift的`highres_tiling=false`，不传global_stream、tile、4GB权重缓存等执行参数。
- Sol-Attn保留为原图稀疏计算加速，Comfy Kitchen保留为稠密后端。ComfyUI正常模型管理和原生稀疏内核内部实现不另行重写。
- 原Self-Lift插件内部长片时间分片也关闭：运行清单携带四项精确部署替换，把latent upscaler调用设为`enable_chunking=False`，内存工作区按完整时间维估算，日志按full模式记录。
- 上述部署替换有原文件SHA、逐项唯一上下文和结果SHA；任一漂移即失败，不猜测修改。上游仓库不改、模型架构/权重不改，不复制另一套推理实现。
- 不设置OOM后偷偷启用省显存的回退。G4是否容纳所有画布/时长/参考组合尚待实际测峰值。

Self-Lift使用并适配自[slmonker/selflift-Avatar](https://github.com/slmonker/selflift-Avatar/tree/460f88629b35f792a2ca68b219f13c1ba46e2ee3)，条件构建依赖[nkxx188/ComfyUI-MiniMaxH3-Easy](https://github.com/nkxx188/ComfyUI-MiniMaxH3-Easy/tree/4b496eedeff944b2f1266c8c92df1b6c89758de0)；各上游LICENSE/NOTICE随固定源码包保留。节点API图按照[Comfy官方API格式](https://docs.comfy.org/development/api-development/workflow-api-format)编译，DynamicCombo使用`selection`与`selection.tau`扁平字段。

## Colab 部署

按[运行清单](workflow-v4-g4-runtime.json)校验插件、权重、依赖和远端磁盘。任务、预算、回执、取回与释放使用同一锁定执行包。
