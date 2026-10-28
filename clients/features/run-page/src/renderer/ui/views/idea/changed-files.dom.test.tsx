/**
 * An attempt's changed files on the idea's page, in a DOM: every file is listed, and opening one
 * shows its own part of the diff with what it adds and removes, or says why it cannot yet.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {settled} from '../../../../state/idea-files.test-helper.js';

const ENCODE = (text: string) => new TextEncoder().encode(text);

import {CHANGED_FILES} from '../../../../state/index.js';
import {ChangedFiles} from './changed-files.js';

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

async function opened(seed: number, read: boolean) {
  const {read: model, idea, paths, kept, diff, hash} = settled(seed, ENCODE);
  const run = model(read ? {[hash]: diff} : {});
  const attempt = run.candidates.candidates.find(each => each.id === idea)?.attempts.at(-1);
  if (attempt === undefined) throw new Error('no attempt');
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<ChangedFiles model={run} attempt={attempt} />));
  const heads = [...container.querySelectorAll('button[aria-expanded]')] as HTMLElement[];
  for (const head of heads) await act(async () => head.click());
  const bodies = [...container.querySelectorAll('.vs-collapsible-body')].map(
    body => body.textContent ?? '',
  );
  await act(async () => root.unmount());
  container.remove();
  return {heads: heads.map(head => head.textContent ?? ''), bodies, paths, kept};
}

test('every file opens its own changes once the diff is read', async () => {
  for (const seed of [0, 1, 2, 3, 5, 10]) {
    const {heads, bodies, paths, kept} = await opened(seed, true);
    expect(heads.length).toBe(paths.length);
    paths.forEach((path, index) => {
      expect(heads[index]).toContain(path);
      if (index < kept) {
        expect(heads[index]).toContain('+2 −1');
        expect(bodies[index]).toContain(`+new ${index}`);
      } else {
        expect(bodies[index]).toBe(CHANGED_FILES.cut);
      }
    });
  }
});

test('before the diff is read each file says it is reading it', async () => {
  const {bodies, kept} = await opened(1, false);
  expect(bodies.slice(0, kept)).toEqual(Array(kept).fill(CHANGED_FILES.reading));
});
