/** `FigureStrip` behavior in a browser DOM, through the package's public entry. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {FigureStrip} from './index.js';

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

async function mount(node: ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(node));
  return container;
}

describe('FigureStrip', () => {
  test('is one labelled landmark with a figure per measure, in order', async () => {
    const container = await mount(
      <FigureStrip
        label="Cost summary"
        figures={[
          {label: 'Total spend', value: '$80.00', detail: '1 machine'},
          {label: 'Runs', value: '1', hint: 'Runs on the host'},
        ]}
      />,
    );
    const strip = container.querySelector('section[aria-label="Cost summary"]');
    expect(strip).not.toBeNull();
    const figures = [...(strip?.querySelectorAll('.vs-figure') ?? [])];
    expect(figures.map(each => each.querySelector('.vs-text')?.textContent)).toEqual([
      'Total spend',
      'Runs',
    ]);
    expect(figures[0]?.querySelector('.vs-figure-detail')?.textContent).toBe('1 machine');
    expect(figures[1]?.querySelector('.vs-figure-detail')).toBeNull();
    expect(figures[1]?.getAttribute('title')).toBe('Runs on the host');
  });

  test('draws a meter only for a figure that has a share, and the problem tone only when told', async () => {
    const container = await mount(
      <FigureStrip
        label="Fleet"
        figures={[
          {label: 'Spend', value: '$412', share: 0.9, tone: 'warn'},
          {label: 'Agents', value: '3'},
          {label: 'Time', value: '—', tone: 'muted'},
        ]}
      />,
    );
    const figures = [...container.querySelectorAll('.vs-figure')];
    expect(figures[0]?.querySelector('.vs-meter')?.getAttribute('data-tone')).toBe('warn');
    expect(figures[1]?.querySelector('.vs-meter')).toBeNull();
    expect(figures[0]?.querySelector('[data-tone="warn"]')).not.toBeNull();
    expect(figures[2]?.querySelector('[data-tone="muted"]')).not.toBeNull();
  });
});
