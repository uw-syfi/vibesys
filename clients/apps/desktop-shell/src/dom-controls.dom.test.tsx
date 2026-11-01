/**
 * The every-control-acts audit checks itself: a control that changes what a person reads passes,
 * a control that changes nothing (a tab that only marks itself selected, a button wired to
 * nothing, a select that snaps back) fails by name, whichever of the controls of a screen it is.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {controlsOf, describeControl, readScreen, use} from './testing/dom-controls.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

/** The controls of the screen `build` draws that change nothing when used, each on a fresh copy. */
async function inert(build: () => HTMLElement): Promise<string[]> {
  const first = build();
  const controls = controlsOf(first);
  first.remove();
  const found: string[] = [];
  for (const control of controls) {
    const copy = build();
    const before = readScreen(copy);
    await use(copy, control);
    if (readScreen(copy) === before) found.push(describeControl(control));
    copy.remove();
  }
  return found;
}

/** A screen drawn by `fill`, wired by `wire`: each call is a fresh copy. */
function draw(
  fill: (screen: HTMLElement) => void,
  wire: (screen: HTMLElement) => void = () => undefined,
): () => HTMLElement {
  return () => {
    const screen = document.createElement('main');
    fill(screen);
    document.body.append(screen);
    wire(screen);
    return screen;
  };
}

function element<T extends HTMLElement>(
  tag: string,
  attributes: Record<string, string>,
  text = '',
): T {
  const made = document.createElement(tag) as T;
  for (const [name, value] of Object.entries(attributes)) made.setAttribute(name, value);
  made.textContent = text;
  return made;
}

for (const snapsBack of [false, true]) {
  test(`the audit reads a grouped select's displayed text${snapsBack ? ' after it snaps back' : ''}`, async () => {
    const build = draw(
      screen => {
        const select = element<HTMLSelectElement>('select', {'aria-label': 'Model'});
        select.append(element('option', {value: 'run'}, 'Same as the run'));
        const group = element('optgroup', {label: 'Models'});
        group.append(element('option', {value: 'other'}, 'Other model'));
        select.append(group);
        screen.append(select);
      },
      screen => {
        const select = screen.querySelector('select') as HTMLSelectElement;
        if (snapsBack) select.addEventListener('change', () => (select.value = 'run'));
      },
    );
    expect(controlsOf(build())).toHaveLength(1);
    expect(await inert(build)).toEqual(snapsBack ? ['combobox "Model"'] : []);
  });
}

test('a tab that only marks itself selected still fails the audit', async () => {
  const build = draw(
    screen => screen.append(element('button', {role: 'tab', 'aria-selected': 'false'}, 'Timeline')),
    screen => {
      const tab = screen.querySelector('button') as HTMLElement;
      tab.addEventListener('click', () => tab.setAttribute('aria-selected', 'true'));
    },
  );
  expect(await inert(build)).toEqual(['tab "Timeline"']);
});

test('property: of any buttons, exactly those wired to nothing fail the audit', async () => {
  await fc.assert(
    fc.asyncProperty(fc.array(fc.boolean(), {minLength: 1, maxLength: 8}), async live => {
      const names = live.map((_, index) => `Button ${index}`);
      const build = draw(
        screen => {
          for (const name of names) screen.append(element('button', {}, name));
          screen.append(element('p', {id: 'out'}));
        },
        screen => {
          const out = screen.querySelector('#out') as HTMLElement;
          screen.querySelectorAll('button').forEach((button, index) => {
            // A live button writes its own name; a dead one only presses itself.
            button.addEventListener('click', () => {
              if (live[index]) out.textContent = names[index] ?? '';
              else button.setAttribute('aria-pressed', 'true');
            });
          });
        },
      );
      const dead = names.filter((_, index) => !live[index]).map(name => `button "${name}"`);
      expect(await inert(build)).toEqual(dead);
    }),
    {numRuns: 25},
  );
});
