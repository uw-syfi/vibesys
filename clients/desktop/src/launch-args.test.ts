import {describe, expect, test} from 'bun:test';
import {LaunchError, parseLaunch} from './launch-args.js';

const CWD = '/home/me';

describe('parseLaunch', () => {
  test('starts a server for a project, passing run arguments through', () => {
    expect(parseLaunch(['--project', 'proj', '--', '--task', 'a b'], undefined, CWD)).toEqual({
      kind: 'start',
      project: '/home/me/proj',
      runArgs: ['--task', 'a b'],
    });
    expect(parseLaunch(['--project=/abs/p'], undefined, CWD)).toEqual({
      kind: 'start',
      project: '/abs/p',
      runArgs: [],
    });
  });

  test('drops the separator pnpm passes before the arguments', () => {
    expect(parseLaunch(['--', '--project', 'p', '--', 'x'], undefined, CWD)).toEqual({
      kind: 'start',
      project: '/home/me/p',
      runArgs: ['x'],
    });
  });

  test('attaches to a running server by socket', () => {
    expect(parseLaunch(['--socket', 's.sock'], '', CWD)).toEqual({
      kind: 'attach',
      socketPath: '/home/me/s.sock',
    });
  });

  test('opens the picker, on a host when one is named', () => {
    expect(parseLaunch([], undefined, CWD)).toEqual({kind: 'picker', host: null});
    expect(parseLaunch(['--'], undefined, CWD)).toEqual({kind: 'picker', host: null});
    expect(parseLaunch(['--host', 'gpu-box'], undefined, CWD)).toEqual({
      kind: 'picker',
      host: {kind: 'ssh', alias: 'gpu-box'},
    });
  });

  test('attaches to a detached run on a host, or on this machine', () => {
    expect(parseLaunch(['--host=gpu-box', '--instance', '0123456789ab'], undefined, CWD)).toEqual({
      kind: 'instance',
      host: {kind: 'ssh', alias: 'gpu-box'},
      instanceId: '0123456789ab',
    });
    expect(parseLaunch(['--instance', '0123456789ab'], undefined, CWD)).toEqual({
      kind: 'instance',
      host: {kind: 'local'},
      instanceId: '0123456789ab',
    });
  });

  test('keeps the gateway window for run-desktop.sh', () => {
    const plan = parseLaunch([], 'http://127.0.0.1:8765/?token=abc', CWD);
    expect(plan.kind).toBe('gateway');
  });

  test('rejects every malformed command line, naming the problem', () => {
    const cases: [readonly string[], string][] = [
      [['--', '--', '--project', 'p'], 'only --project takes run arguments'],
      [['--host', 'h', '--', 'x'], 'only --project takes run arguments'],
      [['--host', '-oProxyCommand=x'], 'not a usable ssh host name'],
      [['--host'], '--host needs a value'],
      [['--instance', 'ZZ'], '--instance needs a run id'],
      [['--host', 'h', '--project', 'p'], 'pass only one of'],
      [['--project'], '--project needs a path'],
      [['--project='], '--project needs a path'],
      [['--project', 'a', '--project', 'b'], '--project is given twice'],
      [['--project', 'a', '--socket', 'b'], 'pass only one of'],
      [['--socket', 's', '--', 'x'], 'only --project takes run arguments'],
      [['--demo'], 'unknown argument --demo'],
      [['proj'], 'unknown argument proj'],
    ];
    for (const [argv, message] of cases) {
      expect(() => parseLaunch(argv, undefined, CWD)).toThrow(message);
      expect(() => parseLaunch(argv, undefined, CWD)).toThrow(LaunchError);
    }
    expect(() => parseLaunch(['--project', 'p'], 'http://127.0.0.1:1/?token=t', CWD)).toThrow(
      'does not take arguments',
    );
  });
});
