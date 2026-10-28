/** The components' behavior in a browser DOM, through the `.` entry only. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {Window} from 'happy-dom';
import {act, type ReactNode, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {
  ActionPanel,
  AppFrame,
  BrandMark,
  Button,
  Chart,
  type ChartPoint,
  ChatMessage,
  Checkbox,
  type ChoiceOption,
  ChoiceTiles,
  type Completion,
  ErrorActions,
  Fact,
  FactLine,
  Field,
  type ParetoAxis,
  ParetoChart,
  type ParetoPoint,
  PathInput,
  Scrubber,
  SegmentedControl,
  Select,
  StackedArea,
  StackedBars,
  StatusDot,
  type StatusTone,
  Table,
  Text,
  TextInput,
  type TextStyle,
} from './index.js';
import {TYPE} from './tokens/index.js';

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

describe('AppFrame', () => {
  const frame = (hidden: boolean) => (
    <AppFrame
      label="Side"
      nav={<Button text="New run" press={() => undefined} />}
      navHidden={hidden}
      control={
        <Button
          icon="sidebar"
          label="Show sidebar"
          hint="Show sidebar (Cmd+K)"
          press={() => undefined}
        />
      }
    >
      <main>Main</main>
    </AppFrame>
  );

  test('shown or hidden, the main area is only the page, with no strip pushing it down; hidden, the navigation is inert, out of the accessibility tree, and the reveal control is in the title-bar row', async () => {
    const container = await mount(frame(false));
    const nav = one(container, 'nav');
    expect(nav.hasAttribute('inert')).toBe(false);
    expect(nav.hasAttribute('aria-hidden')).toBe(false);
    expect(container.querySelector('.vs-app-strip')).toBeNull();
    expect(one(container, '.vs-app-titlebar-control button').getAttribute('aria-label')).toBe(
      'Show sidebar',
    );

    const hidden = await mount(frame(true));
    const hiddenNav = one(hidden, 'nav');
    expect(hiddenNav.hasAttribute('inert')).toBe(true);
    expect(hiddenNav.getAttribute('aria-hidden')).toBe('true');
    expect(hidden.querySelector('.vs-app-strip')).toBeNull();
    const reveal = one(hidden, '.vs-app-titlebar-control button');
    expect(reveal.getAttribute('aria-label')).toBe('Show sidebar');
    expect(reveal.textContent).toBe('');
    expect(one(hidden, 'main').textContent).toBe('Main');
  });
});

describe('Button hint', () => {
  test('a hint is the hover tooltip, beside the accessible name', async () => {
    const container = await mount(
      <Button
        icon="sidebar"
        label="Hide sidebar"
        hint="Hide sidebar (Ctrl+K)"
        press={() => undefined}
      />,
    );
    const button = one(container, 'button');
    expect(button.getAttribute('title')).toBe('Hide sidebar (Ctrl+K)');
    expect(button.getAttribute('aria-label')).toBe('Hide sidebar');
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

  test('a hidden label still names its control and is marked for the stylesheet to hide', async () => {
    const container = await mount(
      <Field label="Sort ideas" hideLabel>
        <Select
          size="sm"
          value="a"
          change={() => undefined}
          options={[{value: 'a', label: 'Newest'}]}
        />
      </Field>,
    );
    const label = one(container, 'label');
    expect(label.getAttribute('for')).toBe(one(container, 'select').id);
    expect(label.textContent).toBe('Sort ideas');
    expect(label.hasAttribute('data-hidden')).toBe(true);
    expect(one(container, '.vs-select').getAttribute('data-size')).toBe('sm');
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

  test('options of one group sit under its heading and each still reports its own value', async () => {
    const chosen: string[] = [];
    const container = await mount(
      <Field label="Model">
        <Select
          value="a"
          change={value => chosen.push(value)}
          options={[
            {value: 'none', label: 'Same as the run'},
            {value: 'a', label: 'A', group: 'Maker X'},
            {value: 'b', label: 'B', group: 'Maker X'},
            {value: 'c', label: 'C', group: 'Maker Y'},
          ]}
        />
      </Field>,
    );
    const groups = [...container.querySelectorAll('optgroup')].map(group => [
      group.label,
      [...group.querySelectorAll('option')].map(option => option.value),
    ]);
    expect(groups).toEqual([
      ['Maker X', ['a', 'b']],
      ['Maker Y', ['c']],
    ]);
    expect(container.querySelectorAll('select > option')).toHaveLength(1);
    const select = one(container, 'select') as HTMLSelectElement;
    await act(async () => {
      select.value = 'c';
      // happy-dom's own Event class: the page's typings name another, which it does not accept.
      select.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
    });
    expect(chosen).toEqual(['c']);
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

describe('Scrubber with one position', () => {
  const range = async (last: number, value: number) => {
    const container = await mount(
      <Scrubber
        label="Run position"
        value={value}
        last={last}
        live
        pastLabel="Replay"
        latestLabel="End"
        position="07:32:14"
        change={() => undefined}
        backToLive={() => undefined}
      />,
    );
    return container.querySelector('input[type="range"]') as HTMLInputElement;
  };

  test('regression: a run with only its end to stand at shows a whole track, the playhead at its end', async () => {
    const only = await range(0, 0);
    expect([only.min, only.max, only.value]).toEqual(['0', '1', '1']);
    expect(only.disabled).toBe(true);
  });

  test('with more positions the track is the positions and the playhead stays where it is put', async () => {
    const some = await range(4, 2);
    expect([some.min, some.max, some.value]).toEqual(['0', '4', '2']);
    expect(some.disabled).toBe(false);
  });
});

describe('Chart emphasis and spans', () => {
  const points: ChartPoint[] = [
    {id: 'a', x: 1, y: 10, filled: true},
    {id: 'b', x: 2, y: 20, filled: true},
    {id: 'c', x: 3, y: 15, filled: false},
  ];
  const frame = (container: HTMLElement) =>
    [...container.querySelectorAll('.vs-chart-pick')].map(pick => [
      pick.getAttribute('aria-label'),
      pick.querySelector('circle.vs-chart-point')?.getAttribute('cx'),
    ]);

  test('emphasis marks dots and the ring follows the focus, with every dot still drawn', async () => {
    const container = await mount(
      <Chart
        label="c"
        points={points.map(point => ({
          ...point,
          emphasis:
            point.id === 'b'
              ? ('focus' as const)
              : point.id === 'a'
                ? ('related' as const)
                : ('dim' as const),
        }))}
        pick={() => undefined}
      />,
    );
    const kinds = [...container.querySelectorAll('.vs-chart-pick')].map(each =>
      each.getAttribute('data-emphasis'),
    );
    expect(kinds).toEqual(['related', 'focus', 'dim']);
    expect(container.querySelectorAll('.vs-chart-ring')).toHaveLength(1);
    expect(container.querySelectorAll('.vs-chart-related')).toHaveLength(1);
    expect(
      container
        .querySelector('.vs-chart-pick[data-emphasis="focus"]')
        ?.getAttribute('aria-pressed'),
    ).toBe('true');
  });

  test('neither emphasis nor spans move a dot or the axes', async () => {
    const xTicks = [
      {value: 0, label: '0'},
      {value: 4, label: '4'},
    ];
    const plain = await mount(
      <Chart label="c" points={points} xTicks={xTicks} pick={() => undefined} />,
    );
    const marked = await mount(
      <Chart
        label="c"
        points={points.map(point => ({...point, emphasis: 'dim' as const}))}
        spans={[{from: -50, to: 2, label: 'an idea'}]}
        xTicks={xTicks}
        pick={() => undefined}
      />,
    );
    expect(frame(marked)).toEqual(frame(plain));
    expect(marked.querySelectorAll('.vs-chart-span')).toHaveLength(1);
    // The band is clipped to the plot: it starts at the axis, not at x = -50.
    const band = marked.querySelector('.vs-chart-span-band');
    const axis = marked.querySelector('line.vs-chart-axis');
    expect(band?.getAttribute('x')).toBe(axis?.getAttribute('x1'));
  });
});

describe('Chart x labels', () => {
  test('a label at the plot’s end stays inside the drawing instead of being cut by it', async () => {
    const container = await mount(
      <Chart
        label="c"
        points={[{x: 0, y: 1, filled: true}]}
        xTicks={[
          {value: 0, label: '0s'},
          {value: 1_200_000, label: '333h 20m'},
        ]}
        pick={() => undefined}
      />,
    );
    const view = container.querySelector('svg.vs-chart')?.getAttribute('viewBox')?.split(' ') ?? [];
    const width = Number(view[2]);
    const [first, last] = [...container.querySelectorAll('text.vs-chart-label')].filter(each =>
      /^\d+[hs]/.test(each.textContent ?? ''),
    );
    expect(last?.getAttribute('text-anchor')).toBe('end');
    expect(Number(last?.getAttribute('x'))).toBeLessThanOrEqual(width);
    expect(first?.getAttribute('text-anchor')).not.toBe('end');
  });
});

describe('FactLine', () => {
  test('lays its facts out in order, a shrinking one marked', async () => {
    const container = await mount(
      <FactLine wrap={false}>
        <Fact>one</Fact>
        <Fact shrink title="a long name">
          a long name
        </Fact>
      </FactLine>,
    );
    const facts = [...container.querySelectorAll('.vs-fact')];
    expect(facts.map(fact => fact.textContent)).toEqual(['one', 'a long name']);
    expect(facts.map(fact => fact.hasAttribute('data-shrink'))).toEqual([false, true]);
    expect(container.querySelector('.vs-factline')?.hasAttribute('data-wrap')).toBe(false);
  });
});

describe('ParetoChart', () => {
  const x: ParetoAxis = {name: 'Throughput', unit: 'ops/s', direction: 'higher'};
  const y: ParetoAxis = {name: 'Latency', unit: 'ms', direction: 'lower'};
  const points: ParetoPoint[] = [
    {id: 'input', label: 'Input', x: 100, y: 50, time: 0, state: 'input'},
    {id: 'a', label: 'Ring buffer', x: 140, y: 45, time: 1, state: 'passed'},
    {id: 'b', label: 'Batching', x: 120, y: 60, time: 2, state: 'passed'},
    {id: 'c', label: 'Padding', x: 130, y: 30, time: 3, state: 'passed'},
    {id: 'd', label: 'Unsafe', x: 300, y: 10, time: 4, state: 'failed'},
  ];
  const roles = (root: HTMLElement): Record<string, string> =>
    Object.fromEntries(
      [...root.querySelectorAll('.vs-chart .vs-pareto-point')].map(point => [
        point.querySelector('title')?.textContent?.split(' · ')[0] ?? '',
        point.getAttribute('data-role') ?? '',
      ]),
    );

  test('marks the input, the frontier, the dominated and the failed', async () => {
    const root = await mount(<ParetoChart label="Trade-off" points={points} x={x} y={y} />);
    expect(roles(root)).toEqual({
      Input: 'input',
      'Ring buffer': 'frontier',
      Batching: 'dominated',
      Padding: 'frontier',
      Unsafe: 'failed',
    });
    expect([...root.querySelectorAll('li')].map(li => li.textContent)).toEqual([
      'Input',
      'On the frontier',
      'Dominated',
      'Failed',
    ]);
    expect(root.querySelector('svg')?.textContent).toContain('lower is better');
  });

  test('draws only the points measured by the cursor, and the frontier of those', async () => {
    const root = await mount(
      <ParetoChart label="Trade-off" points={points} x={x} y={y} cursor={2} />,
    );
    expect(roles(root)).toEqual({
      Input: 'input',
      'Ring buffer': 'frontier',
      Batching: 'dominated',
    });
  });

  test('hovering a point names it with its values; leaving removes the name', async () => {
    const root = await mount(<ParetoChart label="Trade-off" points={points} x={x} y={y} />);
    const point = [...root.querySelectorAll('.vs-pareto-point')].find(p =>
      p.querySelector('title')?.textContent?.startsWith('Padding'),
    );
    await act(async () => {
      point?.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
    });
    expect(root.querySelector('.vs-pareto-hover-text')?.textContent).toBe(
      'Padding · 130 ops/s · 30 ms',
    );
    await act(async () => {
      point?.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
    });
    expect(root.querySelector('.vs-pareto-hover')).toBeNull();
  });

  test('rings the highlighted points', async () => {
    const root = await mount(
      <ParetoChart label="Trade-off" points={points} x={x} y={y} highlighted={['b']} />,
    );
    expect(root.querySelectorAll('.vs-chart-ring')).toHaveLength(1);
  });
});

describe('ParetoChart picking', () => {
  const x: ParetoAxis = {name: 'Throughput', unit: 'ops/s', direction: 'higher'};
  const y: ParetoAxis = {name: 'Latency', unit: 'ms', direction: 'lower'};
  const points: ParetoPoint[] = [
    {id: 'a', label: 'Ring buffer', x: 140, y: 45, time: 1, state: 'passed'},
    {id: 'b', label: 'Batching', x: 120, y: 60, time: 2, state: 'passed'},
    {id: 'c', label: 'Padding', x: 130, y: 30, time: 3, state: 'passed'},
  ];

  test('points are buttons with one tab stop; a press or Enter picks; hover is reported', async () => {
    const picked: string[] = [];
    const hovered: (string | null)[] = [];
    const root = await mount(
      <ParetoChart
        label="Trade-off"
        points={points}
        x={x}
        y={y}
        highlighted={['b']}
        pick={id => picked.push(id)}
        hover={id => hovered.push(id)}
      />,
    );
    const buttons = [...root.querySelectorAll('[role="button"]')];
    expect(buttons).toHaveLength(3);
    expect(buttons.filter(b => b.getAttribute('tabindex') === '0')).toHaveLength(1);
    expect(root.querySelector('[tabindex="0"]')?.getAttribute('aria-label')).toContain('Batching');
    const named = (id: string) =>
      buttons.find(b => b.getAttribute('aria-label')?.startsWith(id)) as Element;
    await act(async () => {
      named('Ring buffer').dispatchEvent(new MouseEvent('click', {bubbles: true}));
    });
    await press(named('Padding') as unknown as HTMLElement, 'Enter');
    expect(picked).toEqual(['a', 'c']);
    hovered.length = 0;
    await act(async () => {
      named('Ring buffer').dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
      named('Ring buffer').dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
    });
    expect(hovered).toEqual(['a', null]);
  });

  test('without pick the chart is one image and nothing is a button', async () => {
    const root = await mount(<ParetoChart label="Trade-off" points={points} x={x} y={y} />);
    expect(root.querySelector('[role="button"]')).toBeNull();
    expect(root.querySelector('svg')?.getAttribute('role')).toBe('img');
  });
});

describe('ParetoChart values', () => {
  test('a hover writes a value exactly, never rounded to hundreds, with a unit', async () => {
    const x: ParetoAxis = {name: 'Throughput', unit: 'ops/s', direction: 'higher'};
    const y: ParetoAxis = {name: 'Latency', unit: '', direction: 'lower'};
    const points: ParetoPoint[] = [
      {id: 'a', label: 'Big', x: 6_054_154, y: 0.512, time: 1, state: 'passed'},
      {id: 'b', label: 'Small', x: 12.34, y: 47.25, time: 2, state: 'passed'},
    ];
    const root = await mount(<ParetoChart label="Trade-off" points={points} x={x} y={y} />);
    const titles = [...root.querySelectorAll('.vs-pareto-point title')].map(t => t.textContent);
    expect(titles).toEqual(['Big · 6,054,154 ops/s · 0.512', 'Small · 12.3 ops/s · 47.3']);
  });
});

function ControlledTiles() {
  const [value, setValue] = useState('a');
  return (
    <ChoiceTiles
      label="Agent"
      value={value}
      change={setValue}
      options={[
        {value: 'a', label: 'Alpha', leading: <BrandMark mark="claudecode" />},
        {value: 'b', label: 'Beta', leading: <BrandMark mark={null} />},
      ]}
    />
  );
}

describe('ChoiceTiles', () => {
  test('is one named radio group; a press chooses exactly one tile, each with its mark and name', async () => {
    const container = await mount(<ControlledTiles />);
    expect(one(container, '[role="radiogroup"]').getAttribute('aria-label')).toBe('Agent');
    const radios = [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    expect(new Set(radios.map(radio => radio.name)).size).toBe(1);
    await click(radios[1] as HTMLInputElement);
    expect(radios.map(radio => radio.checked)).toEqual([false, true]);
    const tiles = [...container.querySelectorAll('.vs-choice-tile')];
    expect(tiles.map(tile => tile.textContent)).toEqual(['Alpha', 'Beta']);
    // A mark is decoration beside its name: hidden from assistive technology.
    expect(tiles.every(tile => tile.querySelector('svg[aria-hidden="true"]') !== null)).toBe(true);
  });
});

function ControlledPanel(props: {readonly startOpen: boolean}) {
  const [open, setOpen] = useState(props.startOpen);
  return (
    <ActionPanel
      label="Ready to start"
      status={{state: 'ok', text: 'Ready · 3 checks passed'}}
      open={open}
      toggle={setOpen}
      summary="queue-rs · mpmc"
      details={<p>Every check passed.</p>}
      action={<Button variant="primary" text="Start run" press={() => undefined} />}
    />
  );
}

describe('ActionPanel', () => {
  test('is a named region whose status opens and closes its details, with the action always there', async () => {
    const container = await mount(<ControlledPanel startOpen={false} />);
    expect(one(container, 'section').getAttribute('aria-label')).toBe('Ready to start');
    const status = one(container, '.vs-action-panel-status');
    expect(status.getAttribute('aria-expanded')).toBe('false');
    expect(container.textContent).not.toContain('Every check passed.');
    expect(container.textContent).toContain('Start run');
    await click(status);
    expect(status.getAttribute('aria-expanded')).toBe('true');
    expect(container.textContent).toContain('Every check passed.');
    expect(container.textContent).toContain('Start run');
    await click(status);
    expect(container.textContent).not.toContain('Every check passed.');
  });
});

describe('ErrorActions', () => {
  const buttons = (container: HTMLElement) =>
    [...container.querySelectorAll('button')].map(button => button.textContent);

  test('renders nothing with nothing to do', async () => {
    const container = await mount(<ErrorActions />);
    expect(container.innerHTML).toBe('');
  });

  test('ask is one button that asks once per press', async () => {
    let asked = 0;
    const container = await mount(<ErrorActions ask={() => (asked += 1)} />);
    expect(buttons(container)).toEqual(['Ask about this error']);
    await click(one(container, 'button'));
    await click(one(container, 'button'));
    expect(asked).toBe(2);
  });

  test('copy writes the text to the clipboard and says it did', async () => {
    // Bun has a navigator of its own, so the page's (with its clipboard) is installed for this test.
    const own = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
    Object.defineProperty(globalThis, 'navigator', {configurable: true, value: window.navigator});
    try {
      const container = await mount(<ErrorActions copy={'a\nb'} />);
      expect(buttons(container)).toEqual(['Copy details']);
      await click(one(container, 'button'));
      expect(await window.navigator.clipboard.readText()).toBe('a\nb');
      expect(buttons(container)).toEqual(['Copied']);
    } finally {
      if (own !== undefined) Object.defineProperty(globalThis, 'navigator', own);
    }
  });

  test('a clipboard that refuses says so, and new text can be copied afresh', async () => {
    const own = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
    const refusing = {clipboard: {writeText: () => Promise.reject(new Error('denied'))}};
    Object.defineProperty(globalThis, 'navigator', {configurable: true, value: refusing});
    try {
      const container = document.createElement('div');
      document.body.append(container);
      const root = createRoot(container);
      await act(async () => root.render(<ErrorActions copy="first" />));
      await click(one(container, 'button'));
      expect(buttons(container)).toEqual(['Could not copy']);
      await act(async () => root.render(<ErrorActions copy="second" />));
      expect(buttons(container)).toEqual(['Copy details']);
    } finally {
      if (own !== undefined) Object.defineProperty(globalThis, 'navigator', own);
    }
  });
});

describe('ChatMessage', () => {
  test('a failed message carries the err tone and its notice; others carry the default tone', async () => {
    const container = await mount(
      <>
        <ChatMessage side="start" text="Refused" tone="err" notice="Failed" />
        <ChatMessage side="start" text="Fine" />
      </>,
    );
    const [failed, fine] = [...container.querySelectorAll<HTMLElement>('.vs-chat-message')];
    expect(failed?.getAttribute('data-tone')).toBe('err');
    expect(failed?.querySelector('.vs-text')?.getAttribute('data-tone')).toBe('err');
    expect(fine?.getAttribute('data-tone')).toBe('default');
  });
});

describe('Text', () => {
  test('draws every role of the type scale from custom properties tokens.css declares', async () => {
    const css = readFileSync(new URL('./tokens.css', import.meta.url), 'utf8');
    const roles = Object.keys(TYPE) as TextStyle[];
    const container = await mount(
      roles.map(role => (
        <Text key={role} variant={role}>
          {role}
        </Text>
      )),
    );
    const drawn = [...container.querySelectorAll<HTMLElement>('.vs-text')];
    expect(drawn).toHaveLength(roles.length);
    roles.forEach((role, index) => {
      const style = (drawn[index] as HTMLElement).getAttribute('style') ?? '';
      for (const part of ['family', 'size', 'line', 'weight', 'tracking', 'transform']) {
        expect(style).toContain(`var(--text-${role}-${part})`);
        expect(css).toContain(`--text-${role}-${part}:`);
      }
    });
  });
});

describe('StackedBars', () => {
  const bar = (id: string, parts: readonly number[]) => ({
    id,
    title: `${id}: ${parts.join('+')}`,
    total: parts.reduce((sum, part) => sum + part, 0),
    segments: parts.map((value, index) => ({key: `${id}-${index}`, series: index, value})),
  });

  test('draws every bar to the tallest total, each segment its share of it', async () => {
    const container = await mount(
      <StackedBars
        label="Usage over time"
        unit="tokens"
        bars={[bar('a', [30, 10]), bar('b', [20])]}
        from="Oct 1"
        to="Oct 2"
      />,
    );
    const heights = [...container.querySelectorAll<HTMLElement>('.vs-bars-bar')].map(each =>
      [...each.querySelectorAll<HTMLElement>('.vs-bars-segment')].map(
        segment => segment.style.height,
      ),
    );
    expect(heights).toEqual([['75%', '25%'], ['50%']]);
    expect(container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
      'Usage over time, in tokens',
    );
    expect(container.textContent).toContain('Oct 1');
    expect(container.textContent).toContain('Oct 2');
  });

  test('draws nothing tall when nothing was spent', async () => {
    const container = await mount(
      <StackedBars label="Usage" unit="tokens" bars={[bar('a', [0])]} />,
    );
    expect(container.querySelector<HTMLElement>('.vs-bars-segment')?.style.height).toBe('0%');
  });
});

describe('StackedArea', () => {
  test('stacks the layers over the slices and names each slice for the pointer', async () => {
    const container = await mount(
      <StackedArea
        label="Running total"
        unit="tokens"
        titles={['first', 'second']}
        layers={[
          {key: 'a', series: 1, values: [10, 20]},
          {key: 'b', series: 2, values: [5, 5], dim: true},
        ]}
      />,
    );
    expect(container.querySelectorAll('.vs-area-layer')).toHaveLength(2);
    expect(container.querySelectorAll('.vs-area-layer[data-dim]')).toHaveLength(1);
    expect(
      [...container.querySelectorAll('.vs-area-slice title')].map(each => each.textContent),
    ).toEqual(['first', 'second']);
  });
});

describe('Table folds and kinds', () => {
  test('a folding row has a named chevron, a sub row and the total cannot be selected', async () => {
    const picked: string[] = [];
    let folded = 0;
    const rows = [
      {id: 'a', kind: undefined},
      {id: 'b', kind: 'sub' as const},
      {id: 'c', kind: 'total' as const},
    ];
    const container = await mount(
      <Table
        label="Rows"
        columns={[{key: 'id', header: 'id', cell: row => row.id}]}
        rows={rows}
        rowKey={row => row.id}
        rowKind={row => row.kind}
        select={key => picked.push(key)}
        fold={row =>
          row.id === 'a' ? {open: false, toggle: () => (folded += 1), label: 'Expand a'} : 'none'
        }
      />,
    );
    const trs = [...container.querySelectorAll<HTMLElement>('tbody tr')];
    await act(async () =>
      container.querySelector<HTMLElement>('button[aria-label="Expand a"]')?.click(),
    );
    for (const row of trs) await act(async () => row.click());
    expect(folded).toBe(1);
    expect(picked).toEqual(['a']);
  });
});

describe('Table control column', () => {
  test('keeps its header as the accessible name in a hidden span and marks its cells', async () => {
    const container = await mount(
      <Table
        label="Rows"
        columns={[
          {key: 'id', header: 'id', cell: row => row.id},
          {key: 'go', header: 'Go', control: true, cell: () => <button type="button">Go</button>},
        ]}
        rows={[{id: 'a'}]}
        rowKey={row => row.id}
      />,
    );
    const headers = [...container.querySelectorAll('th')];
    expect(headers.map(th => th.textContent)).toEqual(['id', 'Go']);
    expect(headers[1]?.querySelector('.vs-table-hidden-header')?.textContent).toBe('Go');
    expect(headers[0]?.querySelector('.vs-table-hidden-header')).toBeNull();
    expect(container.querySelector('td[data-control]')?.textContent).toBe('Go');
    expect(container.querySelectorAll('td[data-control]')).toHaveLength(1);
  });
});
