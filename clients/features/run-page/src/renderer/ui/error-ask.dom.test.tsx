/**
 * The Run screen's errors offer to ask the run chat about them: once per error, only where the app
 * hands it a chat that can ask, and the question is the banner's own words.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {Outcome, RecordSchema, RunFailureKind, type Record as StoreRecord} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  commandErrorReport,
  type ErrorReport,
  endedErrorReport,
  projectSkeleton,
  RUN_ROUTE,
  type RunPageState,
  runPageInitial,
  skeletonScreenState,
} from '../../state/index.js';
import {type RunChatSlot, RunScreen} from './run-screen.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};
const ASK = 'Ask about this error';

/** The `short` fixture's run, ended Failed. */
function failedRun() {
  const records: StoreRecord[] = [...storeRunFixture('short').records];
  records.push(
    create(RecordSchema, {
      seq: BigInt(records.length + 1),
      epoch: 1,
      at: {seconds: 1700001000n},
      body: {
        case: 'lifecycle',
        value: {
          state: {
            case: 'ended',
            value: {
              outcome: Outcome.FAILED,
              failure: {
                kind: RunFailureKind.ENVIRONMENT_UNAVAILABLE,
                reason: 'Git refused the workspace',
              },
            },
          },
        },
      },
    }),
  );
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const whole = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  return {state: skeletonScreenState(SEED, skeleton, whole, runPageInitial(SEED)), whole};
}

/** The Run screen for `state`, and how to take it down again. */
async function shown(state: RunPageState, chat: RunChatSlot) {
  const {whole} = failedRun();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <RunScreen
        state={state}
        model={whole}
        whole={whole}
        core={null}
        route={RUN_ROUTE}
        zoom={() => undefined}
        dispatch={() => undefined}
        chat={chat}
      />,
    ),
  );
  return {
    container,
    unmount: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

function asks(container: HTMLElement): HTMLButtonElement[] {
  return [...container.querySelectorAll('button')].filter(button => button.textContent === ASK);
}

test('the failed banner asks the chat about the failure, in its own words', async () => {
  const {state} = failedRun();
  const asked: ErrorReport[] = [];
  const {container, unmount} = await shown(state, {
    open: false,
    toggle: () => undefined,
    panel: null,
    askAboutError: report => asked.push(report),
  });
  const buttons = asks(container);
  expect(buttons).toHaveLength(1);
  expect(buttons[0]?.closest('.vs-banner')?.textContent).toContain('Git refused the workspace');
  await act(async () => buttons[0]?.click());
  expect(asked).toEqual([endedErrorReport(state) as ErrorReport]);
  await unmount();
});

test('no chat that can ask, no offer', async () => {
  const {state} = failedRun();
  const unavailable = await shown(state, {
    open: false,
    toggle: () => undefined,
    panel: null,
    unavailable: 'This machine cannot serve an ended run',
    askAboutError: () => undefined,
  });
  expect(asks(unavailable.container)).toHaveLength(0);
  await unavailable.unmount();
  const absent = await shown(state, {open: false, toggle: () => undefined, panel: null});
  expect(asks(absent.container)).toHaveLength(0);
  await absent.unmount();
});

test('a command the run did not take asks with its notice', async () => {
  const {state} = failedRun();
  const noticed = {...state, notice: 'The run is not running'};
  const asked: ErrorReport[] = [];
  const {container, unmount} = await shown(noticed, {
    open: false,
    toggle: () => undefined,
    panel: null,
    askAboutError: report => asked.push(report),
  });
  const button = asks(container).find(each =>
    each.closest('.vs-banner')?.textContent?.includes('did not take the command'),
  );
  await act(async () => button?.click());
  expect(asked).toEqual([commandErrorReport(noticed) as ErrorReport]);
  await unmount();
});
