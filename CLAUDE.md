# CLAUDE.md

给在这个仓库里工作的 AI 助手（以及新加入的开发者）的快速上手说明。详细规范以 `docs/` 为准，这里只记录“开工前必须知道”的内容。

## 项目是什么

BHE（Basketball Highlight Editor）：本地分析篮球比赛视频，找出进球候选片段，人工审核后按球员导出集锦。所有处理在本机完成，不上传视频。

```text
导入视频 → 框选篮筐/篮网 ROI → 生成代理 → 分析 → 审核候选（可标记球员）→ 调整片段 → 导出
```

## 架构速览

| 部分 | 目录 | 说明 |
|---|---|---|
| 桌面 UI | `apps/desktop/` | Flutter + Riverpod + go_router。不直接碰 SQLite/FFmpeg/YOLO |
| Engine | `engine/python/basketball_engine/` | Python 子进程，JSON Lines 协议（`docs/architecture/ENGINE_PROTOCOL_V1.md`）|
| 存储 | `storage.py` + `docs/architecture/SQLITE_SCHEMA_V1.sql` | 每个项目一个目录 + `project.db`，产物在项目 `artifacts/` |
| 算法 | `src/basketball_highlight/`、`scripts/` | 检测、事件、候选生成/精修 |
| 导出 | `engine/python/basketball_engine/adapters/export.py` | FFmpeg 精确导出 |
| 共享文案 | `packages/bhe_l10n/` | 中文为源文案，`bhe_text.dart` 提供英文翻译 |
| 移动端 | `apps/mobile/`、`packages/bhe_core`、`packages/bhe_runtime` | 独立实现，不启动 Python Engine |

完整架构：`docs/architecture/ARCHITECTURE_V1.md`；开发流程：`docs/DEVELOPMENT.md`；发布：`docs/RELEASE.md`。

## 关键文件

- `apps/desktop/lib/providers/project_state.dart`：`ProjectNotifier`，几乎所有项目状态和 Engine 调用都在这里；
- `apps/desktop/lib/providers/session_provider.dart`：Engine 启动（`engineBootstrapProvider`）；
- `apps/desktop/lib/providers/player_roster_provider.dart`：跨项目常用球员名单，见 `docs/PLAYER_ROSTER.md`；
- `apps/desktop/lib/features/review/review_screen.dart`：审核工作台（球员选择器 `_PlayerPickerMenuContent`）；
- `apps/desktop/lib/features/import_video/import_video_screen.dart`：导入与 ROI 配置；
- `engine/python/basketball_engine/service.py`：协议 handler；
- `engine/python/basketball_engine/storage.py`：SQLite 读写和旧库兼容迁移；
- `scripts/build_macos_release.sh` / `scripts/prepare_macos_runtime.sh`：打包；
- `scripts/install_macos_app.sh`：本机构建并安装到 `/Applications`。

本机偏好（主题、语言、最近项目目录、常用球员名单）都用 `SharedPreferences`；项目数据都走 Engine。

## 每次改动后的固定流程

1. 测试：
   ```bash
   .venv/bin/python -m pytest -q
   (cd apps/desktop && flutter test)
   ```
2. 构建并安装到 `/Applications/BHE.app`（会先跑测试，再替换旧版本）：
   ```bash
   scripts/install_macos_app.sh
   ```
   - 默认复用 `dist/macos-runtime` 里的 Python/FFmpeg/模型，只同步仓库里的 Engine 代码；
   - 改了 `requirements.txt` 或需要整体重建运行时：`BHE_FULL_RUNTIME=1 BHE_PYTHON_RUNTIME=<便携 Python 目录> scripts/install_macos_app.sh`；
   - `BHE_SKIP_TESTS=1` 跳过测试。
3. 在 `CHANGELOG.md` 的 `[Unreleased]` 下记一笔。
4. 提交（Conventional Commits：`feat:` / `fix:` / `docs:` / `chore:` …）。推送到 GitHub 前先征得仓库所有者同意。

## 本机环境注意事项

- Python 环境：`.venv`（已 gitignore）。如果没有，可用 `dist/local-python/bin/python3 -m venv --system-site-packages .venv && .venv/bin/python -m pip install -r requirements-dev.txt`。
- **`flutter analyze` 在含中文的路径下会崩溃**（Dart 分析服务器的 LSP 解析问题，与代码无关）。需要静态检查时把仓库 rsync 到一个纯 ASCII 路径再运行；`flutter test` 和 `flutter build` 不受影响。
- `dart format` 会重排很多现有文件，提交前只格式化自己改的代码，避免无关的格式化差异。
- 签名的 `.app` 内运行时不能被修改：Engine 以 `PYTHONDONTWRITEBYTECODE=1` 启动，打包时会删除 `__pycache__`。

## 约定

- UI 文案写中文源串，需要英文时在 `packages/bhe_l10n/lib/src/bhe_text.dart` 加翻译（带变量的句子用该文件里的正则模式）。
- 协议或数据库变更时同步 `docs/architecture/` 里的契约文档和测试（见 `docs/DEVELOPMENT.md` 第 8 节）。
- 不提交视频、模型、`build/`、`dist/`、`.venv/`、截图或本机绝对路径。
