// lib/providers/player_roster_provider.dart
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../core/engine_session.dart' show JsonMap;
import 'project_state.dart';

/// 跨项目的常用球员名单。
///
/// 每个项目的 `players` 表仍是事实来源（候选通过外键引用它）；这里只在本机
/// SharedPreferences 中保存一份球员名字列表，让新视频打开时就能直接选择以前
/// 添加过的球员。被选中时才会在当前项目里创建对应的球员记录。
///
/// 名字在新建球员或给片段标记球员时加入名单；在选择器里删除球员时移出名单。
/// 不会自动把打开的项目里的球员全部灌入名单，这样删除后不会被旧项目“复活”。
const String playerRosterPrefsKey = 'bhe.player_roster';

/// 名单中尚未加入当前项目的球员，在选择器里使用这个前缀的虚拟 id。
const String rosterPlayerIdPrefix = 'roster:';

bool isRosterOnlyPlayerId(String? id) =>
    id != null && id.startsWith(rosterPlayerIdPrefix);

String rosterNameFromPlayerId(String id) =>
    id.substring(rosterPlayerIdPrefix.length);

class PlayerRosterNotifier extends Notifier<List<String>> {
  bool _loaded = false;
  final List<String> _pendingAdds = <String>[];

  @override
  List<String> build() {
    _load();
    return const <String>[];
  }

  Future<void> _load() async {
    List<String> saved = const <String>[];
    try {
      final prefs = await SharedPreferences.getInstance();
      saved = prefs.getStringList(playerRosterPrefsKey) ?? const <String>[];
    } catch (_) {}
    _loaded = true;
    final merged = _merge(saved, [...state, ..._pendingAdds]);
    _pendingAdds.clear();
    state = merged;
    if (merged.length != saved.length) await _save();
  }

  /// 把名字加入名单（去重、去首尾空格）。
  Future<void> add(String name) => addAll([name]);

  Future<void> addAll(Iterable<String> names) async {
    final cleaned = names.map((n) => n.trim()).where((n) => n.isNotEmpty);
    if (!_loaded) {
      _pendingAdds.addAll(cleaned);
      return;
    }
    final merged = _merge(state, cleaned);
    if (merged.length == state.length) return;
    state = merged;
    await _save();
  }

  Future<void> remove(String name) async {
    final trimmed = name.trim();
    if (!_loaded) _pendingAdds.remove(trimmed);
    if (!state.contains(trimmed)) return;
    state = [
      for (final n in state)
        if (n != trimmed) n,
    ];
    await _save();
  }

  List<String> _merge(Iterable<String> base, Iterable<String> extra) {
    final seen = <String>{};
    return [
      for (final n in [...base, ...extra])
        if (n.trim().isNotEmpty && seen.add(n.trim())) n.trim(),
    ];
  }

  Future<void> _save() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setStringList(playerRosterPrefsKey, state);
    } catch (_) {
      // 持久化失败时本次运行仍可用，下次启动从项目球员重新补齐。
    }
  }
}

final NotifierProvider<PlayerRosterNotifier, List<String>>
playerRosterProvider = NotifierProvider<PlayerRosterNotifier, List<String>>(
  PlayerRosterNotifier.new,
);

/// 选择器里展示的球员：当前项目球员在前（顺序不变，颜色保持稳定），
/// 之后是名单中尚未加入本项目的球员（虚拟 id，`roster_only: true`）。
final Provider<List<JsonMap>> pickerPlayersProvider = Provider<List<JsonMap>>((
  ref,
) {
  final projectPlayers = ref.watch(projectProvider.select((s) => s.players));
  final roster = ref.watch(playerRosterProvider);
  final projectNames = {
    for (final p in projectPlayers) p['name']?.toString().trim() ?? '',
  };
  final extras = roster.where((name) => !projectNames.contains(name)).toList()
    ..sort((a, b) => a.toLowerCase().compareTo(b.toLowerCase()));
  return [
    ...projectPlayers,
    for (final name in extras)
      <String, dynamic>{
        'id': '$rosterPlayerIdPrefix$name',
        'name': name,
        'roster_only': true,
      },
  ];
});
