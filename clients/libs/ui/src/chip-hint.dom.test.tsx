/**
 * A chip's hint is for everyone: the chip takes the keyboard focus and exposes the hint as its
 * accessible description (drawn under it on hover and on focus by `inputs.css`), not as a `title`
 * only a pointer can read.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {Chip} from './index.js';

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

/** The elements Tab visits in document order: those with a tab index of 0 or more. */
function tabStops(container: HTMLElement): Element[] {
  return [...container.querySelectorAll('*')].filter(
    element => (element as HTMLElement).tabIndex >= 0,
  );
}

async function mounted(label: string, hint: string | undefined): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () =>
    createRoot(container).render(
      <>
        <Chip label={label} {...(hint === undefined ? {} : {hint})} />
        <Chip label="Other" />
      </>,
    ),
  );
  return container;
}

describe('a chip with a hint', () => {
  test('property: Tab reaches it, and its description is the hint, never a title', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.stringMatching(/^[A-Za-z][A-Za-z ]{0,16}$/),
        fc.stringMatching(/^[A-Za-z][A-Za-z .,\n]{0,60}$/),
        async (label, hint) => {
          const container = await mounted(label, hint);
          const chip = container.querySelector('.vs-chip');
          expect(chip?.textContent).toBe(label);
          expect(tabStops(container)).toEqual(chip === null ? [] : [chip]);
          expect(chip?.getAttribute('aria-description')).toBe(hint);
          expect(chip?.hasAttribute('title')).toBe(false);
          (chip as HTMLElement).focus();
          expect(document.activeElement).toBe(chip);
          container.remove();
        },
      ),
      {numRuns: 40, seed: 3515},
    );
  });

  test('without a hint a chip is plain text: not a tab stop and not described', async () => {
    const container = await mounted('Failed', undefined);
    const [first] = container.querySelectorAll('.vs-chip');
    expect(tabStops(container)).toEqual([]);
    expect(first?.hasAttribute('aria-description')).toBe(false);
    expect(first?.hasAttribute('title')).toBe(false);
    container.remove();
  });

  test('the hint is not part of the chip’s text', async () => {
    const container = await mounted('Failed', 'The build broke at step 3.');
    expect(container.querySelector('.vs-chip')?.textContent).toBe('Failed');
    container.remove();
  });
});
