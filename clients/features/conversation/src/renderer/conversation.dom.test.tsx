import {afterAll, beforeAll, expect, test} from 'bun:test';
import {FakeLifecycle, sessionWith} from '@vibesys/run-session/testing';
import {Window} from 'happy-dom';
import {act, StrictMode} from 'react';
import {ConversationPanel} from './index.js';

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
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

test('StrictMode lifecycle keeps the panel active; reconnect retains drafts and changing runs resets them', async () => {
  const {session} = sessionWith(new FakeLifecycle());
  await session.start();
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const render = (runId: string, connected: boolean): void =>
    root.render(
      <StrictMode>
        <ConversationPanel
          session={session}
          runId={runId}
          connected={connected}
          steerAvailable={true}
        />
      </StrictMode>,
    );
  try {
    await act(async () => render('run-1', true));
    await act(async () =>
      container.querySelector<HTMLButtonElement>('.conversation-trigger')?.click(),
    );
    expect(container.querySelector<HTMLDialogElement>('dialog')?.open).toBe(true);
    const composer = container.querySelector<HTMLTextAreaElement>('textarea');
    if (composer === null) throw new Error('composer missing');
    expect(composer.disabled).toBe(false);
    await act(async () => {
      const setValue = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        'value',
      )?.set;
      setValue?.call(composer, 'retained draft');
      composer.dispatchEvent(new window.Event('input', {bubbles: true}) as unknown as Event);
    });
    await act(async () => render('run-1', false));
    expect(composer.disabled).toBe(true);
    expect(container.textContent).toContain('controls are disconnected');
    await act(async () => render('run-1', true));
    expect(composer.disabled).toBe(false);
    expect(composer.value).toBe('retained draft');
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[aria-label="Close conversation"]')?.click(),
    );
    await act(async () =>
      container.querySelector<HTMLButtonElement>('.conversation-trigger')?.click(),
    );
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('retained draft');
    await act(async () => render('run-2', true));
    expect(container.querySelector<HTMLDialogElement>('dialog')?.open).toBe(false);
    await act(async () =>
      container.querySelector<HTMLButtonElement>('.conversation-trigger')?.click(),
    );
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('');
  } finally {
    await act(async () => root.unmount());
    container.remove();
    await session.close();
  }
});
