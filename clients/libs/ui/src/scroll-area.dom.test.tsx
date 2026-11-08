/** The scroll primitive, and that the page's body is drawn through it. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {ScrollArea, type ScrollKind, type ScrollTag} from './components/scroll-area.js';
import {Page} from './index.js';

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
  await act(async () => createRoot(container).render(node));
  return container;
}

const KINDS: readonly ScrollKind[] = ['column', 'feed', 'table'];
const TAGS: readonly ScrollTag[] = ['div', 'section', 'main', 'ul', 'ol', 'pre'];

describe('ScrollArea', () => {
  test.each(
    KINDS.flatMap(kind => TAGS.map(tag => [kind, tag] as const)),
  )('a %s area drawn as <%s> says what it is for, and passes the rest to its element', async (kind, tag) => {
    const container = await mount(
      <ScrollArea kind={kind} as={tag} className="mine" aria-label="Things" tabIndex={0}>
        text
      </ScrollArea>,
    );
    const area = container.querySelector('[data-scroll]');
    expect(area?.tagName.toLowerCase()).toBe(tag);
    expect(area?.getAttribute('data-scroll')).toBe(kind);
    expect(area?.className).toBe('mine');
    expect(area?.getAttribute('aria-label')).toBe('Things');
    expect(area?.getAttribute('tabindex')).toBe('0');
    expect(area?.textContent).toBe('text');
  });

  test('is a div when it is given no element', async () => {
    const container = await mount(<ScrollArea kind="column">x</ScrollArea>);
    expect(container.querySelector('[data-scroll]')?.tagName).toBe('DIV');
  });

  test('a page scrolls its body in a column area, and nothing else of it', async () => {
    const container = await mount(
      <Page title="Runs" state="ready">
        body
      </Page>,
    );
    const areas = [...container.querySelectorAll('[data-scroll]')];
    expect(areas.map(each => each.className)).toEqual(['vs-page-scroll']);
    expect(areas[0]?.getAttribute('data-scroll')).toBe('column');
  });
});
