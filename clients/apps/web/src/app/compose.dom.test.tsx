/** Interactive coverage for the live page's run/campaign toggle: a real `WebSession` (over a
 * Fake transport) drives both the run page and the live campaign dashboard from one subscription,
 * and switching views neither drops the session nor re-subscribes to its single-subscriber
 * transport. */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {event, eventBatch} from '@vibesys/contracts/testing';
import {FakeLifecycle, sessionWith} from '@vibesys/run-session/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import type {Root} from 'react-dom/client';
import {createLiveApp} from './compose.js';
import {createWebThemeController} from './theme.js';

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

/** Let the transport's and the session's promise callbacks run: microtasks only, no timer. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 50; turn += 1) await Promise.resolve();
}

function toggleButton(container: HTMLElement): HTMLButtonElement {
  const found = container.querySelector<HTMLButtonElement>('#run-campaign-toggle');
  if (found === null) throw new Error('run/campaign toggle missing');
  return found;
}

function fakeTheme(): ReturnType<typeof createWebThemeController> {
  return createWebThemeController(
    {dataset: {}, style: {setProperty: () => undefined}},
    {read: () => null, save: () => undefined, remove: () => undefined},
  );
}

test('the run/campaign toggle switches the live page between the two views over one session', async () => {
  const {session, transport} = sessionWith(new FakeLifecycle());
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root: Root = createRoot(container);

  await act(async () => root.render(createLiveApp(session, fakeTheme())));
  await settle();

  expect(container.textContent).toContain('VIBESYS / RUN VIEWER');
  expect(container.querySelector('.campaign-shell')).toBeNull();
  expect(toggleButton(container).textContent).toBe('View campaign');
  expect(transport.subscriptions).toHaveLength(1);

  await act(async () => toggleButton(container).click());
  await settle();

  expect(container.textContent).not.toContain('VIBESYS / RUN VIEWER');
  expect(container.querySelector('.campaign-shell')).not.toBeNull();
  expect(toggleButton(container).textContent).toBe('View run');
  // Toggling views reads the session's already-folded store; it never dials a second
  // subscription against the single-subscriber transport.
  expect(transport.subscriptions).toHaveLength(1);

  const subscription = transport.subscriptions[0];
  if (subscription === undefined) throw new Error('initial subscription did not open');
  await act(async () => {
    subscription.onMessage(
      eventBatch(
        [
          event(2, 'turn_usage_recorded', {
            agent_kind: 'implementer',
            round_label: 'round-1',
            data: {kind: 'turn_usage_recorded', input_tokens: 10, output_tokens: 2},
          }),
        ],
        {store_id: 'store-1', through_sequence: 2, history_after_sequence: 0},
      ),
    );
  });
  await settle();

  // The active-workstream panel renders the round the folded turn just opened.
  expect(container.textContent).toContain('Round 1');
  expect(transport.subscriptions).toHaveLength(1);

  await act(async () => toggleButton(container).click());
  await settle();
  expect(container.textContent).toContain('VIBESYS / RUN VIEWER');

  await act(async () => root.unmount());
  container.remove();
  await session.close();
});
