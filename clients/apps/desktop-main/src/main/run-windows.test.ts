import {expect, test} from 'bun:test';
import type {WelcomeEvent} from '@vibesys/ipc';
import {RunWindows} from './run-windows.js';

class FakeWindow {
  focused = 0;
  readonly sent: string[] = [];
  readonly welcome = {
    currentId: () => null,
    onNewDocument: () => undefined,
    onDestroyed: () => undefined,
  };

  constructor(
    readonly url: string,
    readonly closed: () => void,
  ) {}

  focus(): void {
    this.focused += 1;
  }

  sendWelcome<T>(channel: string, _event: WelcomeEvent<T>): void {
    this.sent.push(channel);
  }
}

function subject() {
  const made: FakeWindow[] = [];
  const windows = new RunWindows<FakeWindow>({
    shellUrl: 'app://vibesys/shell.html',
    create: (url, _title, closed) => {
      const window = new FakeWindow(url, closed);
      made.push(window);
      return window;
    },
  });
  return {windows, made};
}

const ALPHA = {host: 'ssh:chelan4', runId: 'run-alpha'};
const BRAVO = {host: 'local', runId: 'run-bravo'};

test('each run has at most one window, which a second open focuses', () => {
  const {windows, made} = subject();
  expect(windows.open(ALPHA, 'a').created).toBe(true);
  expect(windows.open(BRAVO, 'b').created).toBe(true);
  const again = windows.open(ALPHA, 'a');
  expect(again.created).toBe(false);
  expect(again.window).toBe(made[0] as FakeWindow);
  expect(made.map(window => [window.url, window.focused])).toEqual([
    ['app://vibesys/shell.html?host=ssh%3Achelan4&run=run-alpha', 1],
    ['app://vibesys/shell.html?host=local&run=run-bravo', 0],
  ]);
});

test('a run with its own window is live there until that window closes', () => {
  const {windows, made} = subject();
  expect(windows.showing(ALPHA)).toBeUndefined();
  windows.open(ALPHA, 'a');
  windows.open(BRAVO, 'b');
  expect(windows.showing(ALPHA)).toBe(made[0]);
  windows.sendWelcome('vibesys:welcome:listing', {sequence: 1, value: null});
  expect(made.map(window => window.sent)).toEqual([
    ['vibesys:welcome:listing'],
    ['vibesys:welcome:listing'],
  ]);
  made[0]?.closed();
  expect(windows.showing(ALPHA)).toBeUndefined();
  expect(windows.showing(BRAVO)).toBe(made[1]);
  expect(windows.open(ALPHA, 'a').created).toBe(true);
});

test('same run id on different hosts addresses each exact window', () => {
  const {windows, made} = subject();
  const alpha = {host: 'ssh:alpha', runId: 'shared-run'} as const;
  const bravo = {host: 'ssh:bravo', runId: 'shared-run'} as const;

  windows.open(alpha, 'a');
  windows.open(bravo, 'b');

  expect(windows.showing(alpha)).toBe(made[0]);
  expect(windows.showing(bravo)).toBe(made[1]);
});
