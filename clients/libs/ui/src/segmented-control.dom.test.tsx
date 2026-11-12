/**
 * What a `SegmentedControl` draws as chosen is the owner's value and nothing else: whatever
 * sequence of presses and arrow keys is made on it, with an owner that takes each change or one
 * that refuses some, the one drawn segment is the one whose radio is checked and the one the owner
 * holds. A hover or a focus ring is never the selection's look (the stylesheet's part, read below).
 */
import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {ChoiceTiles, SegmentedControl} from './index.js';

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

const OPTIONS = [
  {value: 'all', label: 'All'},
  {value: 'live', label: 'Live'},
  {value: 'ended', label: 'Ended'},
] as const;

/** The owner: it takes a change unless the test says the next ones are refused. */
function Owner(props: {readonly refuse: {current: boolean}}) {
  const [value, setValue] = useState<string>('all');
  return (
    <>
      <SegmentedControl
        label="Show"
        options={OPTIONS}
        value={value}
        change={next => {
          if (!props.refuse.current) setValue(next);
        }}
      />
      <output>{value}</output>
    </>
  );
}

type Step =
  | {readonly kind: 'press'; readonly at: number}
  | {readonly kind: 'arrow'; readonly by: 1 | -1}
  | {readonly kind: 'refuse'; readonly on: boolean};

const steps: fc.Arbitrary<readonly Step[]> = fc.array(
  fc.oneof(
    fc.record({kind: fc.constant('press' as const), at: fc.nat(OPTIONS.length - 1)}),
    fc.record({kind: fc.constant('arrow' as const), by: fc.constantFrom(1 as const, -1 as const)}),
    fc.record({kind: fc.constant('refuse' as const), on: fc.boolean()}),
  ),
  {maxLength: 25},
);

describe('SegmentedControl selection', () => {
  test('the drawn segment is always the checked radio and the owner value, whatever is pressed', async () => {
    await fc.assert(
      fc.asyncProperty(steps, async sequence => {
        const refuse = {current: false};
        const container = document.createElement('div');
        document.body.append(container);
        const root = createRoot(container);
        await act(async () => root.render(<Owner refuse={refuse} />));
        const radios = [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
        const labels = [...container.querySelectorAll<HTMLElement>('label.vs-segment')];
        let held = 0;
        // The radio the keyboard is on: arrows move it and choose it, wrapping, as a browser does.
        let focused = 0;
        const press = async (at: number): Promise<void> => {
          focused = at;
          await act(async () => {
            (radios[at] as HTMLInputElement).focus();
            (radios[at] as HTMLInputElement).click();
          });
          if (!refuse.current) held = at;
        };
        for (const step of sequence) {
          if (step.kind === 'refuse') refuse.current = step.on;
          else if (step.kind === 'press') await press(step.at);
          else await press((focused + step.by + OPTIONS.length) % OPTIONS.length);
          const drawn = labels.flatMap((label, at) =>
            label.hasAttribute('data-selected') ? [at] : [],
          );
          const checked = radios.flatMap((radio, at) => (radio.checked ? [at] : []));
          expect(drawn).toEqual([held]);
          expect(checked).toEqual([held]);
          expect(container.querySelector('output')?.textContent).toBe(OPTIONS[held]?.value);
        }
        await act(async () => root.unmount());
        container.remove();
      }),
      {numRuns: 40},
    );
  });
});

const css = (name: string): string => readFileSync(new URL(name, import.meta.url), 'utf8');

/** The declarations of the first rule whose selector is exactly `selector`. */
function declarations(selector: string): string {
  const source = css('inputs.css');
  const at = source.indexOf(`\n${selector} {`);
  if (at < 0) throw new Error(`inputs.css has no rule for ${selector}`);
  return source.slice(at, source.indexOf('}', at));
}

describe('SegmentedControl styles', () => {
  test('the selection is drawn from data-selected, and a hover only tints the ground', () => {
    const selected = declarations('.vs-segment[data-selected]');
    expect(selected).toContain('border-color: var(--text);');
    expect(css('inputs.css')).not.toContain('.vs-segment:has(:checked)');
    const hover = declarations('.vs-segment:not([data-selected]):hover');
    expect(hover).toContain('background: var(--panel-2);');
    expect(hover).not.toMatch(/color:\s*var\(--text\)|border|font-weight/);
  });
});

describe('ChoiceTiles selection', () => {
  test('the drawn tile is the checked radio and the owner value, after any presses', async () => {
    await fc.assert(
      fc.asyncProperty(fc.array(fc.nat(OPTIONS.length - 1), {maxLength: 12}), async presses => {
        function Tiles() {
          const [value, setValue] = useState<string | null>(null);
          return <ChoiceTiles label="Tool" options={OPTIONS} value={value} change={setValue} />;
        }
        const container = document.createElement('div');
        document.body.append(container);
        const root = createRoot(container);
        await act(async () => root.render(<Tiles />));
        const radios = [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
        const tiles = [...container.querySelectorAll<HTMLElement>('label.vs-choice-tile')];
        let held: number | null = null;
        for (const at of presses) {
          await act(async () => (radios[at] as HTMLInputElement).click());
          held = at;
          const drawn = tiles.flatMap((tile, index) =>
            tile.hasAttribute('data-selected') ? [index] : [],
          );
          expect(drawn).toEqual([held]);
          expect(radios.flatMap((radio, index) => (radio.checked ? [index] : []))).toEqual([held]);
        }
        if (held === null)
          expect(tiles.some(tile => tile.hasAttribute('data-selected'))).toBe(false);
        await act(async () => root.unmount());
        container.remove();
      }),
      {numRuns: 30},
    );
  });
});
