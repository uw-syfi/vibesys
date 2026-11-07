import {describe, expect, it} from '@effect/vitest';
import {act} from 'react';
import {installDom, mount} from '../../testing/dom/harness.js';
import {NewRun} from './new-run.js';
import type {StartRunRequest} from './start-draft.js';

installDom();

const HOSTS = [
  {key: 'local', label: 'This Mac'},
  {key: 'ssh:gpu-cluster-1', label: 'gpu-cluster-1'},
];

const open = () => {
  const started: {host: string; request: StartRunRequest}[] = [];
  const opened: {host: string; runId: string}[] = [];
  const screen = mount(
    <NewRun
      hosts={HOSTS}
      progress={undefined}
      start={(host, request) => started.push({host, request})}
      retry={() => undefined}
      openRun={(host, runId) => opened.push({host, runId})}
    />,
  );
  return {screen, started, opened};
};

/**
 * Types into a text field as a person does: the field focused, the native value setter, then the
 * input and change events and the key release (happy-dom's React takes a change from `keyup`).
 */
function type(field: HTMLInputElement | HTMLTextAreaElement, text: string): void {
  const prototype = Object.getPrototypeOf(field) as object;
  const set = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
  if (set === undefined) throw new Error('a text field has a value setter');
  const View = field.ownerDocument.defaultView as unknown as {Event: typeof Event};
  act(() => {
    field.focus();
    set.call(field, text);
    for (const name of ['input', 'change', 'keyup'])
      field.dispatchEvent(new View.Event(name, {bubbles: true}));
  });
}

const repository = (screen: ReturnType<typeof mount>): HTMLInputElement => {
  const field = screen.container.querySelector<HTMLInputElement>(
    'input[placeholder="/path/to/repository"]',
  );
  if (field === null) throw new Error('the form is not showing');
  return field;
};

describe('the New run entry', () => {
  it('shows no form until it is pressed, and then the Start run form with its host choice', () => {
    const {screen} = open();
    expect(screen.container.querySelector('input[placeholder="/path/to/repository"]')).toBeNull();
    screen.press(screen.control('New run'));
    expect(repository(screen).value).toBe('');
    expect(screen.controls('Start run')).toHaveLength(1);
    expect(screen.text()).toContain('Host');
    screen.unmount();
  });

  it('keeps what was typed when the form is put away and brought back', () => {
    const {screen} = open();
    screen.press(screen.control('New run'));
    type(repository(screen), '/work/decode-service');
    const dialog = screen.container.querySelector('dialog');
    if (dialog === null) throw new Error('no dialog');
    const View = dialog.ownerDocument.defaultView as unknown as {Event: typeof Event};
    act(() => {
      dialog.dispatchEvent(new View.Event('cancel', {cancelable: true}));
    });
    expect(screen.container.querySelector('input[placeholder="/path/to/repository"]')).toBeNull();
    screen.press(screen.control('New run'));
    expect(repository(screen).value).toBe('/work/decode-service');
    screen.unmount();
  });
});
