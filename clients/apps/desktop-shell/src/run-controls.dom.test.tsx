/**
 * Every control of the Run screen acts: on a finished run, with the run level, an opened idea, and
 * the Timeline, Agents and Usage faces shown, each control found by role is used once on a fresh
 * copy of the screen and must change what the screen shows (`testing/dom-controls.ts`). The screen
 * is the real `GatewayRun` over the byte bridge, drawn from the records the shipping projection
 * makes of the fixture. The same audit ran in Chromium, one browser context per control; here a
 * screen is drawn in a few tens of milliseconds.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {type HostKey, recordedProjectRoot} from '@vibesys/hosts';
import {createProblemBoard} from '@vibesys/problems';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {IDBFactory} from 'fake-indexeddb';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {GatewayRun} from './gateway-run.js';
import {
  type Control,
  controlsOf,
  describeControl,
  readScreen,
  use,
} from './testing/dom-controls.js';
import {wire} from './testing/gateway-wire.js';
import {MemoryAddress} from './testing/memory-address.js';
import {nativeStoreFixture} from './testing/native-records.js';
import {RunPeer} from './testing/run-peer.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'indexedDB', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

/** What the screen asked its window to do (Run again, a window of its own): that is its effect, as the shell's screen change is for the browser. */
const asked: string[] = [];
const NAVIGATION = {
  runAgain: () => void asked.push('runAgain'),
  openWindow: () => void asked.push('openWindow'),
};
const TURNS = 64;

/** Gives what has arrived its turns to reach the stores and the page. */
async function flush(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await act(async () => Promise.resolve());
}

async function until(what: string, held: () => boolean): Promise<void> {
  for (let turn = 0; turn < TURNS; turn += 1) {
    if (held()) return;
    await act(async () => Promise.resolve());
  }
  throw new Error(`${what} did not appear in ${TURNS} turns`);
}

interface Drawn {
  readonly container: HTMLElement;
  readonly close: () => Promise<void>;
}

/** The Run screen of `fixtureName`, drawn once its ideas are listed. */
async function draw(fixtureName: string): Promise<Drawn> {
  asked.length = 0;
  const stored = nativeStoreFixture(fixtureNamed(fixtureName));
  // A fresh copy keeps nothing the last one cached.
  Object.assign(globalThis, {indexedDB: new IDBFactory()});
  const peer = new RunPeer(wire(stored), [...stored.records], false);
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <GatewayRun
        problems={createProblemBoard()}
        projectRoot={recordedProjectRoot('/project')}
        seed={{
          identity: {host: 'local' as HostKey, runId: 'run-a'},
          title: {project: '/project', task: 'mpmc'},
          hostLabel: 'host-a',
          unavailable: {},
        }}
        bridge={peer}
        subscriptions={peer.host.port}
        schedule={new FakeClock().schedule}
        address={new MemoryAddress()}
        navigation={NAVIGATION}
      />,
    ),
  );
  await until(
    'the screen reading what it shows',
    () => container.querySelector('main[data-state="ready"]') !== null && ideas(container) !== null,
  );
  return {
    container,
    close: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

/** What the screen shows and what it asked its window for. */
const shown = (container: HTMLElement): string => `${readScreen(container)}\n${asked.join(',')}`;

const ideas = (container: HTMLElement): Element | null =>
  container.querySelector('[role="listbox"][aria-label="Ideas"] [role="option"]');

async function show(container: HTMLElement, face: string): Promise<void> {
  const choice = container.querySelector<HTMLInputElement>(
    `[role="radiogroup"][aria-label="Show"] input[value="${face}"]`,
  );
  if (choice === null) throw new Error(`the screen offers no ${face} face`);
  await act(async () => choice.click());
  await flush();
}

/** Where on the Run screen the controls are taken from. */
const STARTS: Readonly<Record<string, (container: HTMLElement) => Promise<void>>> = {
  'the run': async () => undefined,
  'an opened idea': async container => {
    const first = ideas(container);
    if (first === null) throw new Error('the run lists no idea');
    await act(async () => (first as HTMLElement).click());
    await until('the idea’s steps', () =>
      [...container.querySelectorAll('[role="listbox"]')].some(each =>
        (each.getAttribute('aria-label') ?? '').endsWith('steps'),
      ),
    );
  },
  'the Timeline face': container => show(container, 'timeline'),
  'the Agents face': container => show(container, 'agents'),
  'the Usage face': container => show(container, 'usage'),
};

/** Controls that change nothing on this run by design, each with why. */
const INERT_BY_DESIGN: readonly {
  readonly start: RegExp;
  readonly name: RegExp;
  readonly why: string;
}[] = [
  {
    start: /opened idea/,
    name: /^Combine the ring with the padding/,
    why: 'the chart mark and the steps entry of the idea that is already open, the first the list names',
  },
  {
    start: /Timeline/,
    name: /^Last (?:hour|day)$/,
    why: 'the range only drops rows older than that, and this run lies within the hour',
  },
];

/** The Usage face needs a run whose turns report usage. */
const fixtureFor = (start: string): string =>
  start === 'the Usage face' ? 'finished-ten-ideas' : 'finished-run';

for (const start of Object.keys(STARTS)) {
  const fixtureName = fixtureFor(start);
  test(`every control of ${start} acts on ${fixtureName}`, async () => {
    const reach = async (): Promise<Drawn> => {
      const drawn = await draw(fixtureName);
      await STARTS[start]?.(drawn.container);
      await flush();
      return drawn;
    };
    const first = await reach();
    const exempt = INERT_BY_DESIGN.filter(each => each.start.test(start));
    const controls: Control[] = controlsOf(first.container).filter(
      control => !exempt.some(each => each.name.test(control.name)),
    );
    await first.close();
    expect(controls.length, 'the screen has controls').toBeGreaterThan(0);
    const inert: string[] = [];
    for (const control of controls) {
      const drawn = await reach();
      try {
        const before = shown(drawn.container);
        await use(drawn.container, control);
        await flush();
        if (shown(drawn.container) === before) {
          inert.push(`${describeControl(control)} changes nothing on the screen`);
        }
      } finally {
        await drawn.close();
      }
    }
    expect(inert, `controls that do nothing:\n${inert.join('\n')}`).toEqual([]);
  });
}
