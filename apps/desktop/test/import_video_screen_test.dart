import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:desktop/components/cs_step_indicator.dart';
import 'package:desktop/features/import_video/import_video_screen.dart';
import 'package:desktop/providers/project_state.dart';
import 'package:desktop/theme/app_theme.dart';

class _RestoredProjectNotifier extends ProjectNotifier {
  @override
  ProjectState build() => const ProjectState(
    video: {'id': 'saved-video', 'width': 1920, 'height': 1080},
    suggestedRoi: Rect.fromLTRB(0.2, 0.2, 0.6, 0.7),
    netRoi: Rect.fromLTRB(0.3, 0.4, 0.5, 0.6),
    roiSource: 'manual',
    workflowDraft: {},
  );
}

class _MissingRoiProjectNotifier extends ProjectNotifier {
  @override
  ProjectState build() => const ProjectState(
    video: {'id': 'missing-roi', 'width': 1920, 'height': 1080},
    workflowDraft: {'step': 2},
  );

  @override
  Future<bool> suggestRoiForCurrentVideo() async {
    state = state.copyWith(
      suggestedRoi: const Rect.fromLTRB(0.2, 0.2, 0.6, 0.7),
      hoopBbox: const Rect.fromLTRB(0.35, 0.3, 0.4, 0.38),
      roiSource: 'auto',
    );
    return true;
  }

  @override
  Future<void> saveWorkflowDraft(Map<String, dynamic> draft) async {}
}

void main() {
  testWidgets('restores saved detection regions without a workflow draft', (
    tester,
  ) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [projectProvider.overrideWith(_RestoredProjectNotifier.new)],
        child: MaterialApp(
          theme: appTheme(Brightness.dark),
          home: const Scaffold(body: ImportVideoScreen()),
        ),
      ),
    );
    await tester.pump();

    final steps = tester.widget<CsStepIndicator>(find.byType(CsStepIndicator));
    expect(steps.steps[2].completed, isTrue);
  });

  testWidgets('retries detection when a draft has no saved regions', (
    tester,
  ) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          projectProvider.overrideWith(_MissingRoiProjectNotifier.new),
        ],
        child: MaterialApp(
          theme: appTheme(Brightness.dark),
          home: const Scaffold(body: ImportVideoScreen()),
        ),
      ),
    );
    await tester.pump();
    await tester.pump();

    final steps = tester.widget<CsStepIndicator>(find.byType(CsStepIndicator));
    expect(steps.steps[2].completed, isTrue);
  });

  test(
    'estimates standard analysis from the selected range and resolution',
    () {
      final estimate = estimateAnalysisDuration(
        startMs: 0,
        endMs: const Duration(minutes: 30).inMilliseconds,
        width: 1920,
        height: 1080,
        mode: 'standard',
      );

      expect(estimate.label, '约 4–9 分钟');
    },
  );

  test('estimates fast analysis shorter than standard analysis', () {
    const args = <String, int>{
      'startMs': 0,
      'endMs': 1800000,
      'width': 1920,
      'height': 1080,
    };
    final standard = estimateAnalysisDuration(
      startMs: args['startMs']!,
      endMs: args['endMs']!,
      width: args['width']!,
      height: args['height']!,
      mode: 'standard',
    );
    final fast = estimateAnalysisDuration(
      startMs: args['startMs']!,
      endMs: args['endMs']!,
      width: args['width']!,
      height: args['height']!,
      mode: 'fast',
    );

    expect(fast.minimum.inSeconds, lessThan(standard.minimum.inSeconds));
    expect(fast.maximum.inSeconds, lessThan(standard.maximum.inSeconds));
  });

  test('collapses a short estimate with identical rounded bounds', () {
    final estimate = estimateAnalysisDuration(
      startMs: 0,
      endMs: 277000,
      width: 1440,
      height: 1080,
      mode: 'fast',
    );

    expect(estimate.label, '约 1 分钟');
  });
}
