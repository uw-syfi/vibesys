/**
 * Core's bookkeeping records (the run's own inputs, its step attempts, its snapshots) are written
 * among the ordinary ones and are no part of what a person reads: whichever of them a log holds
 * and wherever they sit, every face of the Run screen shows the text it shows without them. The
 * screen is the real `GatewayRun` over the byte bridge, drawn from a log by the real reader.
 *
 * Input space: the live and the short run; core records of every kind or a step interrupted and
 * run again; any number of them before each ordinary record and after the last.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {type HostKey, recordedProjectRoot} from '@vibesys/hosts';
import {createProblemBoard} from '@vibesys/problems';
import {
  INTERRUPTED_THEN_RERUN,
  type StoreRunFixture,
  storeRunFixture,
  withCoreRecords,
} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {IDBFactory} from 'fake-indexeddb';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {GatewayRun} from './gateway-run.js';
import {wire} from './testing/gateway-wire.js';
import {MemoryAddress} from './testing/memory-address.js';
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
  Object.assign(globalThis, {window, indexedDB: new IDBFactory(), IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'indexedDB', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

const FACES = ['ideas', 'timeline', 'agents', 'usage', 'logs'] as const;
const NAVIGATION = {runAgain: () => undefined, openWindow: () => undefined};
/** The text of each face of the run `fixture` is, as the screen draws it. */
async function facesOf(fixture: StoreRunFixture): Promise<readonly string[]> {
  const peer = new RunPeer(wire(fixture), [...fixture.records], false);
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
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
    const texts: string[] = [];
    for (const face of FACES) {
      const choice = container.querySelector<HTMLInputElement>(
        `[role="radiogroup"][aria-label="Show"] input[value="${face}"]`,
      );
      if (choice === null) throw new Error(`the screen offers no ${face} face`);
      await act(async () => choice.click());
      texts.push(container.textContent ?? '');
    }
    return texts;
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
}

/** The run `plain` with `placements[i]` core records before its i-th record (see `withCoreRecords`). */
function mixed(
  plain: StoreRunFixture,
  placements: readonly number[],
  bodies: typeof INTERRUPTED_THEN_RERUN | undefined,
): StoreRunFixture {
  const records = withCoreRecords(plain.records, placements, bodies);
  return {
    ...plain,
    records,
    initialCount: plain.initialCount + records.length - plain.records.length,
  };
}

const KINDS = ['short', 'live'] as const;
const plains = Object.fromEntries(KINDS.map(kind => [kind, storeRunFixture(kind)]));
const plainFaces: Record<string, Promise<readonly string[]>> = {};
const faces = (kind: (typeof KINDS)[number]): Promise<readonly string[]> =>
  (plainFaces[kind] ??= facesOf(plains[kind] as StoreRunFixture));

test('property: core records among the ordinary ones change no face of the Run screen', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.constantFrom(...KINDS),
      fc.constantFrom<typeof INTERRUPTED_THEN_RERUN | undefined>(undefined, INTERRUPTED_THEN_RERUN),
      fc
        .array(fc.nat(4), {minLength: 1, maxLength: 40})
        .filter(core => core.some(count => count > 0)),
      async (kind, bodies, placements) => {
        const plain = plains[kind] as StoreRunFixture;
        const records = mixed(plain, placements, bodies);
        expect(records.records.length).toBeGreaterThan(plain.records.length);
        const after = await facesOf(records);
        expect(after).toEqual(await faces(kind));
        for (const text of after)
          expect(text).not.toMatch(/Core input|attempt \d+ started|Run ended/);
      },
    ),
    {
      numRuns: 12,
      examples: [
        ['live', undefined, [2, 1, 0, 3, 1, 2, 4, 1, 2]],
        ['short', INTERRUPTED_THEN_RERUN, [2, 1, 0, 3, 1, 2, 4, 1, 2]],
        ['live', INTERRUPTED_THEN_RERUN, [2, 1, 0, 3, 1, 2, 4, 1, 2]],
        ['short', undefined, [2, 1, 0, 3, 1, 2, 4, 1, 2]],
      ],
    },
  );
});
