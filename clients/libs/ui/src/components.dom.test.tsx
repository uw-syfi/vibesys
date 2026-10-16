/** The components' behavior in a browser DOM, through the `.` entry only. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type ReactNode, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {
  Button,
  Checkbox,
  type ChoiceOption,
  type Completion,
  Field,
  PathInput,
  Scrubber,
  SegmentedControl,
  Select,
  StatusDot,
  type StatusTone,
  TextInput,
} from './index.js';

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

/** A key pressed on `element`, focused first as a key's target always is. */
async function press(element: HTMLElement, key: string): Promise<void> {
  await act(async () => {
    element.focus();
    element.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true}));
  });
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    (element as HTMLElement).click();
  });
}

function one(container: HTMLElement, selector: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(selector);
  if (element === null) throw new Error(`no ${selector}`);
  return element;
}

function ControlledSegments() {
  const [value, setValue] = useState('a');
  const options: readonly ChoiceOption<string>[] = [
    {value: 'a', label: 'First'},
    {value: 'b', label: 'Second'},
    {value: 'c', label: 'Third'},
  ];
  return <SegmentedControl label="Mode" options={options} value={value} change={setValue} />;
}

describe('SegmentedControl', () => {
  test('is one named radio group; a press chooses exactly one segment', async () => {
    const container = await mount(<ControlledSegments />);
    expect(one(container, '[role="radiogroup"]').getAttribute('aria-label')).toBe('Mode');
    const radios = [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    expect(new Set(radios.map(radio => radio.name)).size).toBe(1);
    for (const index of [2, 0, 1]) {
      await click(radios[index] as HTMLInputElement);
      expect(radios.map(radio => radio.checked)).toEqual(radios.map((_, at) => at === index));
    }
  });
});

function ControlledPath(props: {readonly completions: readonly Completion[]}) {
  const [value, setValue] = useState('');
  return (
    <Field label="Folder">
      <PathInput value={value} change={setValue} completions={props.completions} />
    </Field>
  );
}

const COMPLETIONS: readonly Completion[] = [
  {value: '/srv/alpha', detail: '1.0'},
  {value: '/srv/beta'},
  {value: '/srv/gamma'},
];

describe('PathInput', () => {
  test('arrows highlight a completion, Enter takes it, Esc hides the list', async () => {
    const container = await mount(<ControlledPath completions={COMPLETIONS} />);
    const input = one(container, 'input') as HTMLInputElement;
    expect(input.getAttribute('role')).toBe('combobox');
    expect(input.getAttribute('aria-expanded')).toBe('true');
    expect(one(container, 'label').getAttribute('for')).toBe(input.id);

    await press(input, 'ArrowDown');
    await press(input, 'ArrowDown');
    const active = input.getAttribute('aria-activedescendant');
    expect(active).not.toBeNull();
    expect(one(container, `[id="${active}"]`).textContent).toContain('/srv/beta');
    expect(one(container, '[aria-selected="true"]').textContent).toContain('/srv/beta');

    await press(input, 'ArrowUp');
    await press(input, 'ArrowUp');
    expect(one(container, '[aria-selected="true"]').textContent).toContain('/srv/gamma');

    await press(input, 'Enter');
    expect(input.value).toBe('/srv/gamma');
    expect(input.getAttribute('aria-activedescendant')).toBeNull();

    await press(input, 'Escape');
    expect(input.getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    await press(input, 'ArrowDown');
    expect(input.getAttribute('aria-expanded')).toBe('true');
  });

  test('a press on a completion takes it', async () => {
    const container = await mount(<ControlledPath completions={COMPLETIONS} />);
    await click(container.querySelectorAll('[role="option"]')[1] as Element);
    expect((one(container, 'input') as HTMLInputElement).value).toBe('/srv/beta');
  });

  test('with nothing to offer it is a collapsed combobox', async () => {
    const container = await mount(<ControlledPath completions={[]} />);
    const input = one(container, 'input');
    await press(input, 'ArrowDown');
    expect(input.getAttribute('aria-expanded')).toBe('false');
    expect(input.getAttribute('aria-controls')).toBeNull();
  });
});

describe('Button', () => {
  test('a disabled button stays focusable, ignores presses and is described by its reason', async () => {
    let pressed = 0;
    const container = await mount(
      <Button text="Start" press={() => pressed++} disabledReason="Choose a folder first" />,
    );
    const button = one(container, 'button');
    await click(button);
    expect(pressed).toBe(0);
    expect(button.hasAttribute('disabled')).toBe(false);
    expect(button.getAttribute('aria-disabled')).toBe('true');
    const reason = one(container, `[id="${button.getAttribute('aria-describedby')}"]`);
    expect(reason.textContent).toBe('Choose a folder first');
  });

  test('an enabled button presses; an icon-only one is named by its label; href makes a link', async () => {
    let pressed = 0;
    const container = await mount(
      <>
        <Button text="Go" press={() => pressed++} />
        <Button icon="more" label="More actions" press={() => pressed++} />
        <Button text="Docs" href="#docs" />
      </>,
    );
    for (const button of container.querySelectorAll('button')) await click(button);
    expect(pressed).toBe(2);
    expect(one(container, '[aria-label="More actions"]').tagName).toBe('BUTTON');
    expect(one(container, 'a').getAttribute('href')).toBe('#docs');
  });
});

describe('Field', () => {
  test('labels its control and describes it by help and error', async () => {
    const container = await mount(
      <Field label="Name" help="Shown in the list" error="Too long">
        <TextInput value="x" />
      </Field>,
    );
    const input = one(container, 'input');
    expect(one(container, 'label').getAttribute('for')).toBe(input.id);
    expect(input.getAttribute('aria-invalid')).toBe('true');
    const described = (input.getAttribute('aria-describedby') ?? '')
      .split(' ')
      .map(id => one(container, `[id="${id}"]`).textContent);
    expect(described).toEqual(['Shown in the list', 'Too long']);
  });

  test('a control outside a Field refuses to render', async () => {
    const original = console.error;
    console.error = () => undefined;
    try {
      await expect(mount(<TextInput value="" />)).rejects.toThrow('must be inside a Field');
    } finally {
      console.error = original;
    }
  });
});

describe('Select', () => {
  test('a disabled option says why and cannot be chosen', async () => {
    const chosen: string[] = [];
    const container = await mount(
      <Field label="Size">
        <Select
          value="s"
          change={value => chosen.push(value)}
          options={[
            {value: 's', label: 'Small'},
            {value: 'l', label: 'Large', disabledReason: 'needs more memory'},
          ]}
        />
      </Field>,
    );
    const large = one(container, 'option[value="l"]') as HTMLOptionElement;
    expect(large.disabled).toBe(true);
    expect(large.textContent).toBe('Large (needs more memory)');
    expect(chosen).toEqual([]);
  });
});

describe('Checkbox', () => {
  test('reports the new state; a disabled one says why', async () => {
    const changes: boolean[] = [];
    const container = await mount(
      <>
        <Checkbox label="Notify" checked={false} change={value => changes.push(value)} />
        <Checkbox label="Sound" checked={false} change={() => undefined} disabledReason="Muted" />
      </>,
    );
    const [notify, sound] = [...container.querySelectorAll('input')];
    await click(notify as Element);
    expect(changes).toEqual([true]);
    expect((sound as HTMLInputElement).disabled).toBe(true);
    expect(one(container, `[id="${sound?.getAttribute('aria-describedby')}"]`).textContent).toBe(
      'Muted',
    );
  });
});

describe('StatusDot', () => {
  test('is named by its label in every tone', async () => {
    const tones: readonly StatusTone[] = ['neutral', 'live', 'ok', 'warn', 'err'];
    const container = await mount(
      tones.map(tone => <StatusDot key={tone} tone={tone} label={`State ${tone}`} />),
    );
    expect(
      [...container.querySelectorAll('[role="img"]')].map(dot => dot.getAttribute('aria-label')),
    ).toEqual(tones.map(tone => `State ${tone}`));
  });
});

describe('Scrubber', () => {
  test('reads its newest-position words, and offers the way back only in the past', async () => {
    let backs = 0;
    const scrubber = (live: boolean, ended: boolean) => (
      <Scrubber
        label="Run position"
        value={live ? 4 : 2}
        last={4}
        live={live}
        pastLabel="Replay"
        position="16:39:12"
        {...(ended ? {latestLabel: 'End', backLabel: 'Jump to end'} : {})}
        change={() => undefined}
        backToLive={() => {
          backs += 1;
        }}
      />
    );
    const texts = (container: HTMLElement) =>
      [...container.querySelectorAll('button, .vs-chip')].map(node => node.textContent);
    expect(texts(await mount(scrubber(true, false)))).toEqual(['Live']);
    expect(texts(await mount(scrubber(true, true)))).toEqual(['End']);
    const past = await mount(scrubber(false, true));
    expect(texts(past)).toEqual(['Replay', 'Jump to end']);
    await click(past.querySelector('button') as Element);
    expect(backs).toBe(1);
    const range = past.querySelector('input[type="range"]');
    expect(range?.getAttribute('aria-valuetext')).toBe('16:39:12');
  });
});
