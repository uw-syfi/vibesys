/**
 * What an ended run says, as the Run screen draws it. The run's state is the header's: the status
 * chip, a few words of why beside it with the whole of it on the pointer, and Resume and Run again
 * beside the other header actions. Nothing is drawn as a strip across the page, and Logs is a tab.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import type {HostRun} from '@vibesys/hosts';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  briefFailure,
  createRunReader,
  type Route,
  RUN_ROUTE,
  type RunPageAction,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from '../../state/index.js';
import {RunScreen} from './run-screen.js';

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

type Outcome = NonNullable<HostRun['end']>['outcome'];

function ended(outcome: Outcome, reason: string): HostRun {
  const failed = outcome === 'failed' || outcome === 'force_stopped';
  return {
    id: 'a1b2c3d4e5f6',
    projectRoot: '~/vibesys-projects/queue-rs',
    runId: 'run-1',
    status: failed ? 'failed' : 'completed',
    phase: failed ? 'failed' : 'completed',
    paused: null,
    end: {outcome, reason, resumable: true},
    startedAt: 1,
    reason: failed ? reason : null,
  };
}

function pageOf(run: HostRun): RunPageState {
  return runPageStep(
    runPageInitial({
      identity: {host: 'ssh:host-b', runId: 'run-1'},
      title: {project: 'queue-rs', task: null},
      hostLabel: 'host-b',
      unavailable: {},
    }),
    {type: 'record', record: {kind: 'ended', run}},
  ).state;
}

interface Drawn {
  readonly text: string;
  readonly buttons: readonly string[];
  readonly titles: readonly string[];
  readonly disabled: readonly string[];
  readonly marked: readonly string[];
}

/** The screen of `state` at `route`, read for its text, buttons, pointers and marks. */
async function drawn(
  state: RunPageState,
  route: Route = RUN_ROUTE,
  dispatch: (action: RunPageAction) => void = () => undefined,
): Promise<Drawn> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const model = createRunReader().read(state.run.trace, null);
  await act(async () =>
    root.render(
      <RunScreen
        state={state}
        model={model}
        whole={model}
        core={null}
        route={route}
        zoom={() => undefined}
        dispatch={dispatch}
      />,
    ),
  );
  const buttons = [...container.querySelectorAll('button')];
  const read: Drawn = {
    text: container.textContent ?? '',
    buttons: buttons.map(button => button.textContent ?? ''),
    titles: [...container.querySelectorAll('[title]')].map(
      each => each.getAttribute('title') ?? '',
    ),
    disabled: buttons
      .filter(button => button.getAttribute('aria-disabled') === 'true')
      .map(button => button.textContent ?? ''),
    marked: [...container.querySelectorAll('[role="img"]')].map(
      each => each.getAttribute('aria-label') ?? '',
    ),
  };
  await act(async () => root.unmount());
  container.remove();
  return read;
}

const MESSAGE = fc
  .array(fc.stringMatching(/^[a-z]{1,12}$/), {minLength: 1, maxLength: 30})
  .map(words => `${words.join(' ')} failed`);

describe('a failed run', () => {
  test('property: its state is in the header: Failed, a few words of why, the whole of it on the pointer, Resume and Run again', async () => {
    await fc.assert(
      fc.asyncProperty(MESSAGE, async message => {
        const read = await drawn(pageOf(ended('failed', message)));
        const brief = briefFailure(message);
        expect(brief).not.toBeNull();
        expect(read.text).toContain('Failed');
        expect(read.text).toContain(brief ?? '');
        expect(read.titles).toContain(message);
        expect(read.buttons).toContain('Resume');
        expect(read.buttons).toContain('Run again');
        expect(read.buttons).not.toContain('View logs');
        // Only a few words are drawn; the rest is for the pointer.
        if (brief !== message) expect(read.text).not.toContain(message);
      }),
      {numRuns: 25, seed: 3511},
    );
  });

  test('a failure named by its exception reads without the name, and never as a raw code', async () => {
    const message =
      'NestedRepositoryError: host Git refuses a work tree that may hold a nested repository: nested repositories at .cache/uv/sdists-v10/.git';
    const read = await drawn(pageOf(ended('failed', message)));
    expect(read.text).toContain('Host Git refuses a work tree that may hold a nested');
    expect(read.text).not.toContain('NestedRepositoryError');
    expect(read.titles).toContain(message);
  });

  test('a run opened read-only offers Run again and no Resume', async () => {
    const read = await drawn({...pageOf(ended('failed', 'it broke')), readOnly: true});
    expect(read.buttons).toContain('Run again');
    expect(read.buttons).not.toContain('Resume');
  });

  test('the Resume that was asked for waits, with its reason, and the old strip’s words are gone', async () => {
    const state: RunPageState = {
      ...pageOf(ended('failed', 'it broke')),
      resumeStatus: {pending: true, reason: 'Waiting for the run to start'},
    };
    const read = await drawn(state);
    expect(read.disabled).toContain('Resume');
    expect(read.text).not.toContain('Resume requested');
  });
});

describe('a run that ended in a way its status word says', () => {
  test('carries no words beside the status, and still Run again', async () => {
    for (const outcome of ['adopted', 'kept-input', 'no-winner', 'deadline', 'stopped'] as const) {
      const run = ended(outcome, 'a reason the outcome words');
      const read = await drawn(pageOf(run));
      expect(read.titles, outcome).not.toContain('a reason the outcome words');
      expect(read.text, outcome).not.toContain('a reason the outcome words');
      expect(read.buttons, outcome).toContain('Run again');
    }
  });
});

describe('a run whose history cannot be read', () => {
  const unread: RunPageState = {
    ...pageOf(ended('failed', 'it broke')),
    summary: {kind: 'superseded'},
    history: {kind: 'failed', message: 'the helper hung up'},
  };

  test('with logs to look at, says so at the header with the reason on the pointer', async () => {
    const read = await drawn(unread, {...RUN_ROUTE, mode: 'logs'});
    expect(read.text).toContain('History unavailable');
    expect(read.titles.some(title => title.includes('the helper hung up'))).toBe(true);
  });

  test('with nothing else to show, offers Retry and View logs', async () => {
    const read = await drawn(unread);
    expect(read.text).toContain('Could not load this run');
    expect(read.buttons).toContain('Retry');
    expect(read.buttons).toContain('View logs');
  });
});
