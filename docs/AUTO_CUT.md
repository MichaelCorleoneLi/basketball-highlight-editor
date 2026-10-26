# 无界面批量剪辑（待剪辑文件夹）

`scripts/auto_cut_folder.py` 不开桌面端，直接在进程内调用 Engine，把一个文件夹里的比赛视频逐个处理成进球集锦。适合交给 n8n、cron 之类的调度器触发。

## 能做什么、不能做什么

每个视频走和桌面端一样的流程：

1. 建项目（放在 `~/Movies/BasketballProjects/<视频名>-自动剪辑/`，和桌面端默认目录相同，会出现在“最近项目”里）；
2. 标准分析：自动跟踪篮筐、用进球模型判断，不需要框选检测区域；
3. 导出**全部**候选：`<输出目录>/<录制日期YYYYMMDD>/<视频名>-进球集锦.mp4`，单个片段在同目录 `<视频名>-进球片段/`；
4. 清掉代理和检测缓存，只保留 `project.db`。

目前的限制：

- **不识别投篮人**，导出的片段都不分球员。要按人整理，就在桌面端打开自动建好的项目，审核、标记球员后重新导出；
- **没有人工审核**，误检会进集锦。标准分析在已标注视频上覆盖约 95% 的进球，但候选里仍有不少不是进球（见 `docs/research/RIM_EVENTS_MODEL_20261023.md`）；
- 一次只处理一个视频（Engine 同时只允许一个重型任务）。

## 用法

```bash
.venv/bin/python scripts/auto_cut_folder.py start  --inbox <待剪辑> --output <已剪辑>
.venv/bin/python scripts/auto_cut_folder.py status
.venv/bin/python scripts/auto_cut_folder.py run    --inbox <待剪辑> --output <已剪辑>   # 前台运行
```

- `start` 立即返回一行 JSON：`{"started": true, "run_id": ..., "files": [...]}`，或者 `started: false` 加原因（`empty` 没有新视频、`running` 上一次还没跑完、`inbox_missing`）。真正的处理在后台进程里进行，期间用 `caffeinate -i` 防止 Mac 睡眠；
- `status` 输出状态文件 `~/Library/Logs/bhe-auto-cut/status.json`：`state`（`running` / `done` / `crashed` / `idle`）、当前视频和阶段、每个视频的结果、`summary` 汇总；日志在同目录 `auto-cut.log`；
- 只处理 inbox 顶层的视频文件（mp4/mov/m4v/mkv/avi/mts），**2 分钟内改动过的文件视为还在拷贝，这次跳过**；
- 开始处理前，视频会移到 `<inbox>/已处理/`，项目里记录的就是这个位置，之后桌面端打开不用重新关联，下次运行也不会重复处理。要重跑某个视频，把它移回 inbox 顶层即可；
- 某个视频失败不影响后面的视频，结果里记 `status: failed` 和错误信息；没找到进球记 `no_goals`，不导出；
- 通过 SSH 运行时 PATH 里没有 Homebrew，脚本会自动补上 `/opt/homebrew/bin`、`/usr/local/bin` 来找 ffmpeg/ffprobe。

## 本机部署（Jack 的 Mac Studio + 极空间 NAS）

- 待剪辑文件夹：NAS `视频/篮球/待剪辑/`，输出到 `视频/篮球/已剪辑/<日期>/`，和桌面端手动导出的文件放在一起；
- n8n 工作流「篮球自动剪辑」（NAS n8n，手动触发，备份在 NAS `Docker/images/n8n/workflows/basketball-auto-cut.json`）：
  1. SSH 到 Mac 执行 `start`，飞书通知“开始处理 N 个视频”（或“没有待剪辑的视频”“上一次还没跑完”）；
  2. 每 3 分钟 SSH 执行一次 `status`，跑完后飞书推送每个视频的进球数和集锦位置；脚本中途退出或超过 12 小时没完成会推告警；
  3. 飞书通知走共用子流程「飞书通知」，SSH 用凭据「SSH Mac（n8n-healthcheck）」。
- 前提：Mac 开机、已登录（极空间客户端挂载着 NAS），仓库里有 `.venv`。

改了脚本的命令行参数或输出格式时，同步改 n8n 工作流里两个 Code 节点的解析逻辑。
