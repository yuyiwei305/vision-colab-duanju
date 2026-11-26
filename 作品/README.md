# 作品目录

每部新作品使用一个独立子目录；例如 `作品/新作品/`。在工作区根目录运行：

```bash
bash scripts/drama project init '作品/新作品' --title '新作品' --language zh-CN
bash scripts/drama status '作品/新作品'
```

每部作品独立保存配置、输入、创作文档、媒体和运行账本，不复制另一部作品的生产状态或默认事实。具体规则见[工作区说明](../README.md)和[项目约定](../AGENTS.md)。本目录下的作品默认不纳入 Git，记得另行备份。
