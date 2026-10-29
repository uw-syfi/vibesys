/** The DOM test setup: a run-page renderer test mounts React under happy-dom and reads the page. */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EmptyState} from '@vibesys/ui';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';

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

test('a component renders into the DOM', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<EmptyState title="No logs to show" body="Nothing yet." />));
  expect(container.textContent).toContain('No logs to show');
  await act(async () => root.unmount());
});
