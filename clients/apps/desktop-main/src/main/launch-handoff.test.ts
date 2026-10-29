import {describe, expect, test} from 'bun:test';
import {
  LaunchHandoffError,
  planFromSecondInstance,
  prepareLaunchHandoff,
  type SecondInstanceLaunch,
} from './launch-handoff.js';

const SECONDARY_CWD = '/work/from-secondary';
const NOISY_EVENT_ARGV = ['/path/to/electron', '/path/to/app', '--original-process-start-time=123'];

function forwarded(additionalData: unknown): SecondInstanceLaunch {
  return {
    electronArgv: NOISY_EVENT_ARGV,
    electronCwd: '/wrong/event/cwd',
    additionalData,
  };
}

describe('single-instance launch handoff', () => {
  test('preserves every launch variant while ignoring Electron event arguments', () => {
    const cases = [
      {
        argv: [],
        plan: {kind: 'main-window'},
      },
      {
        argv: ['--instance', '0123456789ab'],
        plan: {kind: 'instance', host: {kind: 'local'}, instanceId: '0123456789ab'},
      },
      {
        argv: ['--host', 'gpu-box', '--instance', '0123456789ab'],
        plan: {
          kind: 'instance',
          host: {kind: 'ssh', alias: 'gpu-box'},
          instanceId: '0123456789ab',
        },
      },
      {
        argv: ['--project', 'project', '--', '--task', 'a b'],
        plan: {
          kind: 'start',
          project: '/work/from-secondary/project',
          runArgs: ['--task', 'a b'],
        },
      },
    ] as const;

    for (const {argv, plan} of cases) {
      const prepared = prepareLaunchHandoff(argv, SECONDARY_CWD);
      expect(prepared.plan).toEqual(plan);
      expect(planFromSecondInstance(forwarded(prepared.handoff))).toEqual(plan);
    }
  });

  test('keeps exact run arguments and never appends Chromium event flags', () => {
    const exactRunArgs = [
      '',
      'two words',
      '--original-process-start-time=the-user-really-passed-this',
    ];
    const prepared = prepareLaunchHandoff(
      ['--project', '/work/project', '--', ...exactRunArgs],
      SECONDARY_CWD,
    );

    expect(planFromSecondInstance(forwarded(prepared.handoff))).toEqual({
      kind: 'start',
      project: '/work/project',
      runArgs: exactRunArgs,
    });
  });

  test('owns the exact arguments recorded before Electron receives them', () => {
    const argv = ['--project', 'before'];
    const prepared = prepareLaunchHandoff(argv, SECONDARY_CWD);
    argv[1] = 'after';
    argv.push('--original-process-start-time=123');

    expect(prepared.handoff).toEqual({
      version: 1,
      argv: ['--project', 'before'],
      cwd: SECONDARY_CWD,
    });
    expect(planFromSecondInstance(forwarded(prepared.handoff))).toEqual({
      kind: 'start',
      project: '/work/from-secondary/before',
      runArgs: [],
    });
  });

  test('rejects malformed envelopes with path-specific diagnostics', () => {
    const cases: readonly [unknown, string][] = [
      [null, 'launch handoff must be an object'],
      [[], 'launch handoff must be an object'],
      [{version: 1, argv: [], cwd: SECONDARY_CWD, extra: true}, 'launch handoff.extra is unknown'],
      [{argv: [], cwd: SECONDARY_CWD}, 'launch handoff.version is required'],
      [{version: 2, argv: [], cwd: SECONDARY_CWD}, 'launch handoff.version must be 1'],
      [{version: '1', argv: [], cwd: SECONDARY_CWD}, 'launch handoff.version must be 1'],
      [{version: 1, cwd: SECONDARY_CWD}, 'launch handoff.argv is required'],
      [{version: 1, argv: '--host', cwd: SECONDARY_CWD}, 'launch handoff.argv must be an array'],
      [
        {version: 1, argv: ['--host', 7], cwd: SECONDARY_CWD},
        'launch handoff.argv[1] must be a string',
      ],
      [{version: 1, argv: []}, 'launch handoff.cwd is required'],
      [{version: 1, argv: [], cwd: 7}, 'launch handoff.cwd must be a non-empty string'],
      [{version: 1, argv: [], cwd: ''}, 'launch handoff.cwd must be a non-empty string'],
    ];

    for (const [additionalData, message] of cases) {
      expect(() => planFromSecondInstance(forwarded(additionalData))).toThrow(message);
      expect(() => planFromSecondInstance(forwarded(additionalData))).toThrow(LaunchHandoffError);
    }
  });

  test('malformed additional data never falls back to otherwise valid event values', () => {
    const eventOnlyLaunch: SecondInstanceLaunch = {
      electronArgv: ['/electron', '/app', '--project', '/event/project'],
      electronCwd: '/event/cwd',
      additionalData: undefined,
    };

    expect(() => planFromSecondInstance(eventOnlyLaunch)).toThrow(
      'launch handoff must be an object',
    );
  });
});
