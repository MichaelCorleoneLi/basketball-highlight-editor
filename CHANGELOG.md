# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，版本遵循语义化版本约定。

## [Unreleased]

### Added

- 桌面端常用球员名单：添加过的球员跨视频、跨重启保留，在审核页选择球员时直接可选，可在选择器中删除（`docs/PLAYER_ROSTER.md`）；
- `scripts/install_macos_app.sh`：本机测试、构建 Release 版并替换 `/Applications/BHE.app`；
- 根目录 `CLAUDE.md`：架构速览、关键文件、构建安装流程和本机环境注意事项；
- 单独导出的文件按 `球员名-日期-序号.mp4` 命名（每个球员单独计数，未标记为“未知球员”）；
- 导入配置页在已有项目缺少检测区域时自动重新识别篮筐，并提供“重新识别”按钮；
- 增加中英文根 README、入门、FAQ、开发和发布入口；
- 增加文档任务索引、模型/数据授权说明、第三方 notices 和开源审计记录；
- 增加源码树公开前检查脚本；
- 增加模型、数据、标签和截图目录的本地使用说明。

### Fixed

- 审核页给片段标记球员后立即显示，不再等 Engine 写入和整表刷新；写入失败会回滚并提示，其他操作进行中时标记也不会被丢弃；
- Engine 在后台线程抽取封面帧，标记球员、审核等请求不再排在耗时的 FFmpeg 抽帧后面（视频在 NAS 上时曾要等十几秒）；
- macOS 运行时补充 `dill` 依赖，并在打包检查中真正加载一次模型；
- 签名的 App 内不再生成 `__pycache__`，避免破坏签名；
- Engine 启动失败会把错误传给调用方并记录在状态中。

### Changed

- macOS 最低系统版本提升到 12.0；
- 将 macOS 打包说明合并到 `docs/RELEASE.md`；
- 将第三方许可证研究信息合并到 `docs/THIRD_PARTY_NOTICES.md`；
- 将单样本分析基准移动到 `docs/benchmarks/`，与用户运行文档分开；
- README 明确桌面端、Android 和 iOS 当前实现边界，不把移动端或二进制发布写成已完成。

### Known limitations

- 当前不是包含 Python、FFmpeg、模型和原生 Runtime 的最终用户安装包；
- 模型权重、训练数据、真实比赛视频、截图和原生二进制仍需独立核验授权；
- macOS/Windows 分发、iOS 本地分析和移动端真机验收仍未完成。
