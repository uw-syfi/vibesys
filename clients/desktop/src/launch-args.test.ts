import {describe, expect, test} from 'bun:test';
import {LaunchError, parseLaunch} from './launch-args.js';

const CWD = '/home/me';

describe('parseLaunch', () => {
  test('starts a server for a project, passing run arguments through', () => {
    expect(parseLaunch(['--project', 'proj', '--', '--task', 'a b'], undefined, CWD)).toEqual({
      kind: 'start',
      serverArgs: ['--project', '/home/me/proj', '--task', 'a b'],
    });
    expect(parseLaunch(['--project=/abs/p'], undefined, CWD)).toEqual({
      kind: 'start',
      serverArgs: ['--project', '/abs/p'],
    });
  });

  test('attaches to a running server by socket', () => {
    expect(parseLaunch(['--socket', 's.sock'], '', CWD)).toEqual({
      kind: 'attach',
      socketPath: '/home/me/s.sock',
    });
  });

  test('keeps the gateway window for run-desktop.sh', () => {
    const plan = parseLaunch([], 'http://127.0.0.1:8765/?token=abc', CWD);
    expect(plan.kind).toBe('gateway');
  });

  test('rejects every malformed command line, naming the problem', () => {
    const cases: [readonly string[], string][] = [
      [[], 'pass --project or --socket'],
      [['--project'], '--project needs a path'],
      [['--project='], '--project needs a path'],
      [['--project', 'a', '--project', 'b'], '--project is given twice'],
      [['--project', 'a', '--socket', 'b'], 'not both'],
      [['--socket', 's', '--', 'x'], '--socket does not take run arguments'],
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
