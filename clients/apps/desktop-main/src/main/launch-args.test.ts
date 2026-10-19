import {describe, expect, test} from 'bun:test';
import {LaunchError, parseLaunch, USER_DATA_ENV, userDataOverride} from './launch-args.js';

const CWD = '/home/me';

describe('parseLaunch', () => {
  test('starts a server for a project, passing run arguments through', () => {
    expect(parseLaunch(['--project', 'proj', '--', '--task', 'a b'], CWD)).toEqual({
      kind: 'start',
      project: '/home/me/proj',
      runArgs: ['--task', 'a b'],
    });
    expect(parseLaunch(['--project=/abs/p'], CWD)).toEqual({
      kind: 'start',
      project: '/abs/p',
      runArgs: [],
    });
  });

  test('drops the separator pnpm passes before the arguments', () => {
    expect(parseLaunch(['--', '--project', 'p', '--', 'x'], CWD)).toEqual({
      kind: 'start',
      project: '/home/me/p',
      runArgs: ['x'],
    });
  });

  test('opens the main window when nothing is asked for', () => {
    expect(parseLaunch([], CWD)).toEqual({kind: 'main-window'});
    expect(parseLaunch(['--'], CWD)).toEqual({kind: 'main-window'});
  });

  test('attaches to a detached run on a host, or on this machine', () => {
    expect(parseLaunch(['--host=gpu-box', '--instance', '0123456789ab'], CWD)).toEqual({
      kind: 'instance',
      host: {kind: 'ssh', alias: 'gpu-box'},
      instanceId: '0123456789ab',
    });
    expect(parseLaunch(['--instance', '0123456789ab'], CWD)).toEqual({
      kind: 'instance',
      host: {kind: 'local'},
      instanceId: '0123456789ab',
    });
  });

  test('rejects every malformed command line, naming the problem', () => {
    const cases: [readonly string[], string][] = [
      [['--', '--', '--project', 'p'], 'only --project takes run arguments'],
      [['--host', 'h', '--', 'x'], 'only --project takes run arguments'],
      [['--host', '-oProxyCommand=x'], 'not a usable ssh host name'],
      [['--host'], '--host needs a value'],
      [['--host', 'gpu-box'], '--host needs --instance'],
      [['--instance', 'ZZ'], '--instance needs a run id'],
      [['--host', 'h', '--project', 'p'], 'pass only one of'],
      [['--project'], '--project needs a path'],
      [['--project='], '--project needs a path'],
      [['--project', 'a', '--project', 'b'], '--project is given twice'],
      [['--project', 'a', '--host', 'b'], 'pass only one of'],
      [['--host', 's', '--', 'x'], 'only --project takes run arguments'],
      [['--demo'], 'unknown argument --demo'],
      [['proj'], 'unknown argument proj'],
    ];
    for (const [argv, message] of cases) {
      expect(() => parseLaunch(argv, CWD)).toThrow(message);
      expect(() => parseLaunch(argv, CWD)).toThrow(LaunchError);
    }
  });
});

describe('userDataOverride', () => {
  test('is the default directory unless the variable names one', () => {
    expect(userDataOverride({})).toBeNull();
    expect(userDataOverride({[USER_DATA_ENV]: ''})).toBeNull();
    expect(userDataOverride({[USER_DATA_ENV]: '/work/profile'})).toBe('/work/profile');
  });

  test('refuses a relative directory, naming the variable', () => {
    expect(() => userDataOverride({[USER_DATA_ENV]: 'profile'})).toThrow(LaunchError);
    expect(() => userDataOverride({[USER_DATA_ENV]: 'profile'})).toThrow(USER_DATA_ENV);
  });
});
