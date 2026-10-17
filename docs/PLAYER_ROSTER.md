# 常用球员名单（跨项目）

## 要解决的问题

每个视频是一个独立项目（`<projects_root>/<name>/project.db`），球员原本只存在项目自己的 `players` 表里。打开新视频时名单是空的，每次都要重新“新建球员”。

## 设计

两层存储，职责分开：

| 层 | 位置 | 作用 |
|---|---|---|
| 项目球员 | 项目 `project.db` 的 `players` 表 | 事实来源。`candidate_reviews.player_id` 外键引用它，导出命名、按球员筛选导出都只认这一层 |
| 常用球员名单 | 本机 SharedPreferences，键 `bhe.player_roster`（字符串列表） | 只保存名字，跨项目、跨重启。和主题、语言、最近项目目录同一套本机偏好存储 |

没有改 Engine 协议和 SQLite schema；名单完全在桌面端实现。

代码：

- `apps/desktop/lib/providers/player_roster_provider.dart`
  - `playerRosterProvider`：读写名单（`add` / `addAll` / `remove`，去首尾空格、去重；偏好读取完成前的写入会先排队再合并）；
  - `pickerPlayersProvider`：给选择器用的列表 = 当前项目球员（原顺序，颜色不变）+ 名单里尚未加入本项目的名字（按名字排序）。后者使用虚拟 id `roster:<名字>` 和 `roster_only: true`。
- `ProjectNotifier.ensureProjectPlayer(name)`（`project_state.dart`）：项目里有同名球员就返回其 id，否则静默创建；遇到 `PLAYER_ALREADY_EXISTS` 时刷新列表按名字取 id。
- `review_screen.dart`：`_resolvePlayerId` 把选择器 id 解析成真实项目球员 id，再调用原有的 `setCandidatePlayer` / `setCandidatesPlayer`。

## 行为规则

- **加入名单**：新建球员时；给片段（单个或批量）选中某个球员时。旧项目里已有的球员只要被用过一次就会进入名单。
- **不自动灌入**：打开项目不会把该项目全部球员写入名单，这样在选择器里删除的名字不会因为打开旧项目而“复活”。
- **从名单选择**：选中 `roster:` 条目时先在当前项目创建同名球员，再标记片段。
- **删除**：
  - 删除名单专属条目 → 只从名单移除，不影响任何项目；
  - 删除项目球员 → 走原有删除（该项目里被标记的片段变为未标记），成功后同时从名单移除；其他项目中的同名球员保持不变。
- 名字比较按去掉首尾空格后的完全匹配，与 Engine 的 `UNIQUE(project_id, name)` 一致。

## 测试

- `apps/desktop/test/providers/player_roster_provider_test.dart`：持久化、去重、加载前写入、删除、选择器合并顺序；
- `apps/desktop/test/providers/project_state_test.dart`：`ensureProjectPlayer` 复用与创建。

## 手动验证

1. 打开视频 A，在审核页给一个片段“新建球员”：`老张`；
2. 打开（或新建）视频 B，点片段上的球员标签：列表里能看到 `老张`，选中后片段被标记；
3. 退出并重新打开 App，再次打开任意项目，`老张` 仍在列表里；
4. 在选择器里点 `老张` 右侧的删除图标，确认后它从名单消失；再打开视频 A，`老张` 仍是 A 的项目球员（因为是 A 自己的数据），但只有用到它时才会重新进入名单。
