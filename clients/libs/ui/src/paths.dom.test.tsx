/** No toast draws a filesystem path: a path is reduced to its last name. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {Chip, Toast, withoutPaths} from './index.js';

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

/** What is not a path, whatever its slashes: it stays as written. */
const PLAIN = [
  'Could',
  'not',
  'read',
  'Reading runs',
  'input/output',
  '4/5',
  'and/or',
  '/help',
  'https://example.com/device',
  'tokens/s',
] as const;

const NAME = fc.stringMatching(/^[a-z][a-z0-9_-]{0,8}(?:\.[a-z]{1,4})?$/);

/** A path in some shape, and the name it is shown by. */
const PATH = fc.oneof(
  fc.array(NAME, {minLength: 2, maxLength: 6}).map(names => ({
    text: `/${names.join('/')}`,
    shown: names.at(-1) ?? '',
  })),
  fc.array(NAME, {minLength: 1, maxLength: 4}).map(names => ({
    text: `~/${names.join('/')}`,
    shown: names.at(-1) ?? '',
  })),
  fc
    .tuple(fc.constantFrom('./', '../', '../../'), fc.array(NAME, {minLength: 1, maxLength: 4}))
    .map(([lead, names]) => ({text: `${lead}${names.join('/')}`, shown: names.at(-1) ?? ''})),
  fc
    .tuple(fc.constantFrom('C', 'D'), fc.array(NAME, {minLength: 1, maxLength: 4}))
    .map(([drive, names]) => ({
      text: `${drive}:\\${names.join('\\')}`,
      shown: names.at(-1) ?? '',
    })),
);

/** A sentence of plain words and paths, and the same sentence with each path shown by its name. */
const SENTENCE = fc
  .array(
    fc.oneof(
      fc.constantFrom(...PLAIN).map(text => ({text, shown: text})),
      PATH,
    ),
    {minLength: 1, maxLength: 8},
  )
  .map(parts => ({
    text: parts.map(part => part.text).join(' '),
    shown: parts.map(part => part.shown).join(' '),
  }));

async function mount(node: ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () => createRoot(container).render(node));
  return container;
}

describe('a path is shown by its last name', () => {
  test('property: every path in a sentence is reduced, and nothing else changes', () => {
    fc.assert(
      fc.property(SENTENCE, ({text, shown}) => {
        expect(withoutPaths(text)).toBe(shown);
      }),
      {numRuns: 400, seed: 3},
    );
  });

  test('a temporary folder, a home folder and a drive are reduced, a URL is not', () => {
    expect(
      withoutPaths('Could not read /var/folders/zz/T/vibesys-ab12/hosts.json: unreadable.'),
    ).toBe('Could not read hosts.json: unreadable.');
    expect(withoutPaths('Nothing is at ~/vibesys on host-b any more.')).toBe(
      'Nothing is at vibesys on host-b any more.',
    );
    expect(withoutPaths('host-b:/data/vibesys and C:\\Users\\me\\proj (see /a/b)')).toBe(
      'host-b:vibesys and proj (see b)',
    );
    expect(withoutPaths('Open https://example.com/device and enter the code AB12')).toBe(
      'Open https://example.com/device and enter the code AB12',
    );
  });

  test('property: a toast draws no path in its title or its detail', async () => {
    await fc.assert(
      fc.asyncProperty(SENTENCE, SENTENCE, async (title, detail) => {
        const container = await mount(
          <Toast
            tone="err"
            title={title.text}
            detail={detail.text}
            open
            toggle={() => undefined}
            dismiss={() => undefined}
          />,
        );
        const drawn = container.textContent ?? '';
        expect(drawn).toContain(title.shown);
        expect(drawn).toContain(detail.shown);
        expect(drawn).toBe(`${title.shown}${detail.shown}`);
        container.remove();
      }),
      {numRuns: 60, seed: 4},
    );
  });

  test('property: a chip’s pointer draws no path', async () => {
    await fc.assert(
      fc.asyncProperty(SENTENCE, async hint => {
        const container = await mount(<Chip label="Not read" hint={hint.text} />);
        expect(container.querySelector('.vs-chip')?.getAttribute('title')).toBe(hint.shown);
        container.remove();
      }),
      {numRuns: 60, seed: 5},
    );
  });
});
