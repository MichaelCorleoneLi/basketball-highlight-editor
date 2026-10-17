// test/providers/player_roster_provider_test.dart
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:desktop/core/engine_session.dart' show JsonMap;
import 'package:desktop/providers/player_roster_provider.dart';
import 'package:desktop/providers/project_state.dart';

class _PlayersProjectNotifier extends ProjectNotifier {
  @override
  ProjectState build() => const ProjectState(
    players: <JsonMap>[
      <String, dynamic>{'id': 'player_a', 'name': '老张'},
    ],
  );
}

Future<void> _settle() => Future<void>.delayed(Duration.zero);

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
  });

  test(
    'loads saved names and persists added names without duplicates',
    () async {
      SharedPreferences.setMockInitialValues(<String, Object>{
        playerRosterPrefsKey: <String>['罗斯'],
      });
      final container = ProviderContainer();
      addTearDown(container.dispose);
      container.read(playerRosterProvider);
      await _settle();
      expect(container.read(playerRosterProvider), <String>['罗斯']);

      final notifier = container.read(playerRosterProvider.notifier);
      await notifier.add('  科比 ');
      await notifier.add('罗斯');
      expect(container.read(playerRosterProvider), <String>['罗斯', '科比']);

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getStringList(playerRosterPrefsKey), <String>['罗斯', '科比']);
    },
  );

  test('names added before preferences load are kept', () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      playerRosterPrefsKey: <String>['罗斯'],
    });
    final container = ProviderContainer();
    addTearDown(container.dispose);
    final notifier = container.read(playerRosterProvider.notifier);
    await notifier.add('科比');
    await _settle();
    expect(container.read(playerRosterProvider), <String>['罗斯', '科比']);
  });

  test('remove drops the name from the saved roster', () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      playerRosterPrefsKey: <String>['罗斯', '科比'],
    });
    final container = ProviderContainer();
    addTearDown(container.dispose);
    container.read(playerRosterProvider);
    await _settle();
    await container.read(playerRosterProvider.notifier).remove('罗斯');

    expect(container.read(playerRosterProvider), <String>['科比']);
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getStringList(playerRosterPrefsKey), <String>['科比']);
  });

  test('picker lists project players first, then roster-only names', () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      playerRosterPrefsKey: <String>['老张', '科比', '安哥'],
    });
    final container = ProviderContainer(
      overrides: [projectProvider.overrideWith(_PlayersProjectNotifier.new)],
    );
    addTearDown(container.dispose);
    container.read(pickerPlayersProvider);
    await _settle();

    final players = container.read(pickerPlayersProvider);
    expect(players.map((p) => p['name']), <String>['老张', '安哥', '科比']);
    expect(players.first['id'], 'player_a');
    expect(isRosterOnlyPlayerId(players[1]['id'] as String), isTrue);
    expect(rosterNameFromPlayerId(players[1]['id'] as String), '安哥');
  });
}
