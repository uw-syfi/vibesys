/** The public host picker in a browser DOM: focus and active-option accessibility. */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {HostPicker} from '@vibesys/feature-hosts/renderer';
import {
  PICKER_CLOSED,
  type PickerAction,
  type PickerState,
  pickerStep,
} from '@vibesys/feature-hosts/state';
import {Window} from 'happy-dom';
import {act} from 'react';
import type {Root} from 'react-dom/client';

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

interface MountedPicker {
  readonly container: HTMLElement;
  readonly root: Root;
  readonly loadingToken: number;
  dispatch(action: PickerAction): Promise<void>;
}

async function mountPicker(): Promise<MountedPicker> {
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  let state: PickerState = pickerStep(PICKER_CLOSED, {type: 'open'}).state;
  if (state.kind !== 'open' || state.aliases.kind !== 'loading') {
    throw new Error('opening the host picker did not start its alias request');
  }
  const loadingToken = state.aliases.token;
  const render = (): void => root.render(<HostPicker state={state} visible picker={picker} />);
  const picker = (action: PickerAction): void => {
    state = pickerStep(state, action).state;
    render();
  };
  await act(async () => render());
  return {
    container,
    root,
    loadingToken,
    dispatch: async action => act(async () => picker(action)),
  };
}

async function unmount(mounted: MountedPicker): Promise<void> {
  await act(async () => mounted.root.unmount());
  mounted.container.remove();
}

function combobox(container: HTMLElement): HTMLInputElement {
  const field = container.querySelector<HTMLInputElement>('[role="combobox"]');
  if (field === null) throw new Error('the host picker has no combobox');
  return field;
}

test('arrow keys expose the selected host while focus remains in the combobox', async () => {
  const mounted = await mountPicker();
  await mounted.dispatch({
    type: 'aliases-reply',
    token: mounted.loadingToken,
    result: {ok: true, value: ['alpha', 'beta']},
  });
  const field = combobox(mounted.container);
  field.focus();
  const listboxId = field.getAttribute('aria-controls');
  expect(field.getAttribute('aria-label')).toBe('Search hosts');
  expect(field.getAttribute('aria-expanded')).toBe('true');
  expect(field.getAttribute('aria-autocomplete')).toBe('list');
  expect(listboxId).not.toBeNull();
  expect(document.getElementById(listboxId ?? '')?.getAttribute('role')).toBe('listbox');
  const optionIds = [...mounted.container.querySelectorAll<HTMLElement>('[role="option"]')].map(
    option => option.id,
  );
  expect(optionIds.every(id => id !== '')).toBeTrue();
  expect(new Set(optionIds).size).toBe(optionIds.length);
  const first = field.getAttribute('aria-activedescendant');
  expect(first).not.toBeNull();
  expect(document.getElementById(first ?? '')?.getAttribute('aria-selected')).toBe('true');

  await act(async () => {
    field.dispatchEvent(
      new window.KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}) as unknown as Event,
    );
  });

  const second = field.getAttribute('aria-activedescendant');
  expect(document.activeElement).toBe(field);
  expect(field.getAttribute('aria-controls')).toBe(listboxId);
  expect(second).not.toBe(first);
  expect(second?.startsWith(`${listboxId}-option-`)).toBeTrue();
  expect(document.getElementById(second ?? '')?.textContent).toContain('beta');
  expect(document.getElementById(second ?? '')?.getAttribute('aria-selected')).toBe('true');
  await unmount(mounted);
});

test('loading and zero-result comboboxes have no active descendant', async () => {
  const mounted = await mountPicker();
  const field = combobox(mounted.container);
  expect(field.hasAttribute('aria-activedescendant')).toBeFalse();
  expect(document.getElementById(field.getAttribute('aria-controls') ?? '')).not.toBeNull();

  await mounted.dispatch({
    type: 'aliases-reply',
    token: mounted.loadingToken,
    result: {ok: true, value: []},
  });

  expect(field.hasAttribute('aria-activedescendant')).toBeFalse();
  expect(mounted.container.textContent).toContain('No matching hosts.');
  await unmount(mounted);
});
