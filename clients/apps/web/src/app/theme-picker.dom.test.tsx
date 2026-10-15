/** Public app-shell appearance interactions over an injected in-memory preference implementation. */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import type {Root} from 'react-dom/client';
import {
  createWebThemeController,
  WEB_THEME_NAMES,
  type WebThemeName,
  type WebThemePreferenceStore,
} from './theme.js';
import {WebThemePicker} from './theme-picker.js';

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

class FakeThemePreferences implements WebThemePreferenceStore {
  value: string | null;

  constructor(value: string | null = null) {
    this.value = value;
  }

  read(): string | null {
    return this.value;
  }

  save(theme: WebThemeName): void {
    this.value = theme;
  }

  remove(): void {
    this.value = null;
  }
}

interface MountedPicker {
  readonly root: Root;
  readonly container: HTMLElement;
  readonly preferences: FakeThemePreferences;
}

async function mountPicker(stored: string | null = null): Promise<MountedPicker> {
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const preferences = new FakeThemePreferences(stored);
  const theme = createWebThemeController(document.documentElement, preferences);
  await act(async () => root.render(<WebThemePicker theme={theme} />));
  return {root, container, preferences};
}

async function unmount({root, container}: MountedPicker): Promise<void> {
  await act(async () => root.unmount());
  container.remove();
}

test('renders the generated choices as one named native keyboard control', async () => {
  const mounted = await mountPicker();
  const select = mounted.container.querySelector<HTMLSelectElement>('select');
  if (select === null) throw new Error('theme select missing');

  expect(select.labels.item(0)?.textContent).toBe('Theme');
  expect([...select.options].map(option => option.value)).toEqual([...WEB_THEME_NAMES]);
  expect(select.getAttribute('aria-describedby')).not.toBeNull();
  select.focus();
  expect(document.activeElement).toBe(select);
  expect(select.tabIndex).toBe(0);

  await unmount(mounted);
});

test('selection and reset update the document, visible state, and injected preference', async () => {
  const mounted = await mountPicker();
  const select = mounted.container.querySelector<HTMLSelectElement>('select');
  const reset = [...mounted.container.querySelectorAll('button')].find(button =>
    button.textContent?.includes('Use host default'),
  );
  if (select === null || reset === undefined) throw new Error('theme controls missing');

  await act(async () => {
    select.value = 'high-contrast-light';
    select.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
  });
  expect(document.documentElement.dataset['theme']).toBe('high-contrast-light');
  expect(mounted.preferences.value).toBe('high-contrast-light');
  expect(mounted.container.textContent).toContain('Browser override. Host default: Dark.');
  expect(reset.disabled).toBe(false);

  await act(async () => {
    reset.dispatchEvent(new window.MouseEvent('click', {bubbles: true}) as unknown as Event);
  });
  expect(document.documentElement.dataset['theme']).toBe('dark');
  expect(mounted.preferences.value).toBeNull();
  expect(mounted.container.textContent).toContain('Using host default: Dark.');
  expect(reset.disabled).toBe(true);

  await unmount(mounted);
});
