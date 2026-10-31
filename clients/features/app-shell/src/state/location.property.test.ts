import {describe, expect, test} from 'bun:test';
import type {HostKey} from '@vibesys/hosts';
import fc from 'fast-check';
import {
  fragmentOf,
  type Screen,
  SIDEBAR_EMPTY,
  type SidebarState,
  screenOf,
  sidebarStep,
} from './index.js';

const HOSTS: readonly HostKey[] = ['local', 'ssh:host-a', 'ssh:host-b'];
const RUN_IDS = ['20261015-163047', '20261015-163048', 'r1'];
const ZOOMS = ['', 'mode=logs', 'idea=i1', 'idea=a%2Fb&step=s%261', 'mode=usage'];

const run = fc.record({host: fc.constantFrom(...HOSTS), runId: fc.constantFrom(...RUN_IDS)});

const screen: fc.Arbitrary<Screen> = fc.oneof(
  fc.constant<Screen>({kind: 'runs'}),
  fc.constant<Screen>({kind: 'machines'}),
  fc.constant<Screen>({kind: 'usage'}),
  fc.constant<Screen>({kind: 'settings'}),
  fc
    .constantFrom(...HOSTS)
    .map((machine): Screen => ({kind: 'settings', machine, section: 'logins'})),
  fc.constant<Screen>({kind: 'new-run'}),
  fc.constantFrom(...HOSTS).map((machine): Screen => ({kind: 'new-run', machine})),
  run.map((again): Screen => ({kind: 'new-run', again})),
  fc
    .tuple(run, fc.constantFrom(...ZOOMS))
    .map(
      ([each, zoom]): Screen =>
        zoom === '' ? {kind: 'run', run: each} : {kind: 'run', run: each, zoom},
    ),
);

type Move = {readonly go: Screen} | 'back' | 'forward';

const move: fc.Arbitrary<Move> = fc.oneof(
  {weight: 3, arbitrary: screen.map((go): Move => ({go}))},
  {weight: 1, arbitrary: fc.constantFrom<Move>('back', 'forward')},
);

/** A window: the sidebar's state over a browser's session history of fragments. */
class Walk {
  state: SidebarState = SIDEBAR_EMPTY;
  entries: string[] = [''];
  at = 0;
  readonly visited = new Set(['']);

  go(to: Screen): void {
    const {state, requests} = sidebarStep(this.state, {type: 'select', screen: to});
    this.state = state;
    for (const request of requests) {
      if (request.kind !== 'push-address') continue;
      const fragment = fragmentOf(request.screen);
      this.entries = [...this.entries.slice(0, this.at + 1), fragment];
      this.at += 1;
      this.visited.add(fragment);
    }
  }

  /** Back (-1) or Forward (1); false at the end of the history, where a window stays. */
  step(by: -1 | 1): boolean {
    const fragment = this.entries[this.at + by];
    if (fragment === undefined) return false;
    this.at += by;
    const {state, requests} = sidebarStep(this.state, {
      type: 'restored',
      screen: screenOf(fragment),
    });
    expect(requests.filter(request => request.kind === 'push-address')).toEqual([]);
    this.state = state;
    return true;
  }

  get shown(): string {
    return fragmentOf(this.state.selection);
  }
}

function walk(moves: readonly Move[]): Walk {
  const window = new Walk();
  for (const each of moves) {
    if (each === 'back') window.step(-1);
    else if (each === 'forward') window.step(1);
    else window.go(each.go);
    // The address and the shown screen never disagree.
    expect(window.shown).toBe(window.entries[window.at] ?? '');
    // Back never lands on a location that was not visited, and one move is one entry.
    expect(window.visited.has(window.shown)).toBe(true);
    expect(
      window.entries.slice(1).filter((entry, index) => entry === window.entries[index]),
    ).toEqual([]);
  }
  return window;
}

describe('the window’s location', () => {
  test('property: after any navigation, the address and the shown screen agree', () => {
    fc.assert(
      fc.property(fc.array(move, {maxLength: 30}), moves => {
        walk(moves);
      }),
      {numRuns: 100, seed: 3201},
    );
  });

  test('property: Back to the start then Forward to the end retraces every location', () => {
    fc.assert(
      fc.property(fc.array(move, {maxLength: 30}), moves => {
        const window = walk(moves);
        const entries = [...window.entries];
        const end = window.at;
        const back: string[] = [window.shown];
        while (window.step(-1)) back.push(window.shown);
        expect(back).toEqual(entries.slice(0, end + 1).reverse());
        const forward: string[] = [window.shown];
        while (window.step(1)) forward.push(window.shown);
        expect(forward).toEqual(entries);
      }),
      {numRuns: 100, seed: 3202},
    );
  });

  test('property: a location reads back as itself, without what it was opened for', () => {
    fc.assert(
      fc.property(screen, shown => {
        const fragment = fragmentOf(shown);
        expect(fragmentOf(screenOf(fragment))).toBe(fragment);
        const {machine, section, again} = screenOf(fragment) as Record<string, unknown>;
        expect([machine, section, again]).toEqual([undefined, undefined, undefined]);
        if (shown.kind === 'run') expect(screenOf(fragment)).toMatchObject({run: shown.run});
        else expect(screenOf(fragment).kind).toBe(shown.kind);
      }),
      {numRuns: 200, seed: 3203},
    );
  });

  test('a fragment this app did not write is home', () => {
    for (const fragment of ['nonsense', 'run=', 'run=local', 'run=%E0%A4%A/r1', 'run=local/a/b']) {
      expect(screenOf(fragment)).toEqual({kind: 'runs'});
    }
  });

  test('opening a run shows it from the top, whatever run was zoomed before', () => {
    const first = {host: 'local', runId: 'r1'} as const;
    const window = new Walk();
    window.go({kind: 'run', run: first, zoom: 'idea=i1'});
    window.go({kind: 'runs'});
    window.go({kind: 'run', run: {host: 'local', runId: 'r2'}});
    expect(window.shown).toBe('run=local/r2');
    window.go({kind: 'runs'});
    window.go({kind: 'run', run: first});
    expect(window.shown).toBe('run=local/r1');
  });

  test('Settings opened again or for a machine is one entry, so one Back leaves it', () => {
    const from = {kind: 'run', run: {host: 'ssh:host-a', runId: 'r1'}, zoom: 'idea=i1'} as const;
    const window = new Walk();
    window.go({kind: 'machines'});
    window.go(from);
    window.go({kind: 'settings'});
    window.go({kind: 'settings'});
    window.go({kind: 'settings', machine: 'ssh:host-a', section: 'clusters'});
    expect(window.step(-1)).toBe(true);
    expect(window.state.selection).toEqual(from);
  });

  test('going Back to New run does not repeat what it was opened for', () => {
    const window = new Walk();
    window.go({kind: 'new-run', again: {host: 'local', runId: 'r1'}});
    window.go({kind: 'machines'});
    window.step(-1);
    expect(window.state.selection).toEqual({kind: 'new-run'});
  });
});
