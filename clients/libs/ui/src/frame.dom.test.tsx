/** The frame components (screen, panes, pick lists, meter, gantt, chart picks) in a DOM, through `.` only. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type ReactNode, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {
  Breadcrumb,
  Chart,
  ChartColumns,
  ChartLegend,
  ChartPanel,
  DotLegend,
  Gantt,
  type GanttRow,
  LogPane,
  Meter,
  MetricTile,
  MetricTiles,
  Narrative,
  NewBelow,
  Pane,
  PickList,
  PickRow,
  Said,
  Screen,
  SearchBox,
  Sparkline,
  SwatchLegend,
  Thought,
  Thread,
  type ThreadItem,
  ToolGroup,
  type ToolLine,
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

interface Mounted {
  readonly container: HTMLElement;
  readonly render: (node: ReactNode) => Promise<void>;
}

async function mount(node: ReactNode): Promise<Mounted> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const render = (next: ReactNode): Promise<void> => act(async () => root.render(next));
  await render(node);
  return {container, render};
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
  });
}

/** A key pressed on `element`, focused first as a key's target always is. */
async function press(element: HTMLElement, key: string): Promise<void> {
  await act(async () => {
    element.focus();
    element.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true}));
  });
}

function all(container: HTMLElement, selector: string): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(selector)];
}

function one(container: HTMLElement, selector: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(selector);
  if (element === null) throw new Error(`no ${selector}`);
  return element;
}

const noop = (): void => undefined;

describe('Screen', () => {
  test('is comfortable unless told otherwise, and says its density to the components inside', async () => {
    const plain = await mount(<Screen header="Head">body</Screen>);
    expect(one(plain.container, 'main').getAttribute('data-density')).toBe('comfortable');
    const compact = await mount(
      <Screen header="Head" density="compact">
        body
      </Screen>,
    );
    expect(one(compact.container, 'main').getAttribute('data-density')).toBe('compact');
    expect(compact.container.textContent).toBe('Headbody');
    const inner = await mount(
      <Screen header="Head" landmark="none">
        body
      </Screen>,
    );
    expect(inner.container.querySelector('main')).toBeNull();
    expect(one(inner.container, '.vs-screen').getAttribute('data-density')).toBe('comfortable');
  });
});

describe('Breadcrumb', () => {
  test('links every level but the last, which names where the reader is', async () => {
    const visited: string[] = [];
    const {container} = await mount(
      <Breadcrumb
        label="Zoom"
        items={['Run', 'candidate', 'step'].map((id, index) => ({
          id,
          label: id,
          ...(index === 2 ? {} : {press: () => visited.push(id)}),
        }))}
      />,
    );
    expect(one(container, 'nav').getAttribute('aria-label')).toBe('Zoom');
    const links = all(container, 'button');
    expect(links.map(link => link.textContent)).toEqual(['Run', 'candidate']);
    await click(links[1] as HTMLElement);
    expect(visited).toEqual(['candidate']);
    expect(one(container, '[aria-current="location"]').textContent).toBe('step');
  });

  test('separates levels with quiet marks that are neither read nor focusable', async () => {
    const {container} = await mount(
      <Breadcrumb
        label="Zoom"
        items={['Run', 'candidate', 'step'].map((id, index) => ({
          id,
          label: id,
          ...(index === 2 ? {} : {press: noop}),
        }))}
      />,
    );
    const separators = all(container, '.vs-crumb-sep');
    expect(separators).toHaveLength(2);
    for (const separator of separators) {
      expect(separator.getAttribute('aria-hidden')).toBe('true');
      expect(separator.querySelector('button, a, [tabindex]')).toBeNull();
      expect(separator.textContent).toBe('/');
    }
    // Only the two earlier levels act; the current level is plain text, and no bare chevron
    // button stands beside the crumbs.
    expect(all(container, 'button')).toHaveLength(2);
    expect(one(container, '[aria-current="location"]').tagName).toBe('SPAN');
    expect(all(container, 'svg')).toHaveLength(0);
  });
});

describe('Pane', () => {
  const pane = (enterKey: string | undefined, focusHeading?: boolean) => (
    <Pane
      kicker="steps"
      title="Title"
      width="list"
      {...(enterKey === undefined ? {} : {enterKey})}
      {...(focusHeading === undefined ? {} : {focusHeading})}
    >
      body
    </Pane>
  );

  test('is a region named by its heading, with its kicker, title and body', async () => {
    const {container} = await mount(pane(undefined));
    const region = one(container, 'section');
    const heading = one(container, 'h2');
    expect(region.getAttribute('aria-labelledby')).toBe(heading.id);
    expect(region.textContent).toContain('steps');
    expect(heading.textContent).toBe('Title');
    expect(one(container, '.vs-pane-body').textContent).toBe('body');
  });

  test('takes the focus on its heading once per opening, and not for a live update', async () => {
    const {container, render} = await mount(pane('a'));
    expect(document.activeElement).toBe(one(container, 'h2'));
    const elsewhere = document.createElement('button');
    document.body.append(elsewhere);
    elsewhere.focus();
    await render(pane('a'));
    expect(document.activeElement).toBe(elsewhere);
    await render(pane('b'));
    expect(document.activeElement).toBe(one(container, 'h2'));
    elsewhere.focus();
    await render(pane('c', false));
    expect(document.activeElement).toBe(elsewhere);
  });

  test('without an enter key it neither animates nor takes the focus', async () => {
    const elsewhere = document.createElement('button');
    document.body.append(elsewhere);
    elsewhere.focus();
    const {container} = await mount(pane(undefined));
    expect(document.activeElement).toBe(elsewhere);
    expect(one(container, 'section').hasAttribute('data-entering')).toBe(false);
  });
});

function Candidates(props: {
  readonly selected?: string;
  readonly picked?: string[];
  readonly hovered?: string[];
}) {
  const ids = ['a', 'b', 'c'];
  return (
    <PickList
      label="Candidates"
      {...(props.selected === undefined ? {} : {selected: props.selected})}
      header={{start: 'candidate', end: [{label: 'vs input'}]}}
    >
      {ids.map(id => (
        <PickRow
          key={id}
          id={id}
          title={id}
          guides={id === 'c' ? ['blank', 'elbow'] : []}
          state={{tone: 'ok', label: `State of ${id}`}}
          badges={[{label: `x${id}`, tone: 'neutral'}]}
          bar={{value: 0.5, tone: 'ok', label: `Bar of ${id}`}}
          figure="1.0x"
          path={id === 'b'}
          hover={on => props.hovered?.push(`${id}:${on}`)}
          select={() => props.picked?.push(id)}
        />
      ))}
    </PickList>
  );
}

describe('PickList', () => {
  test('is a named listbox whose rows are options, with one selected and one tab stop', async () => {
    const {container} = await mount(<Candidates selected="b" />);
    const list = one(container, '[role="listbox"]');
    expect(list.getAttribute('aria-label')).toBe('Candidates');
    const rows = all(container, '[role="option"]');
    expect(rows.map(row => row.getAttribute('aria-selected'))).toEqual(['false', 'true', 'false']);
    expect(rows.filter(row => row.tabIndex === 0).map(row => row.textContent)).toHaveLength(1);
    expect(rows[1]?.tabIndex).toBe(0);
    expect(list.tabIndex).toBe(-1);
  });

  test('with nothing selected the list is the tab stop and hands the focus to its first row', async () => {
    const {container} = await mount(<Candidates />);
    const list = one(container, '[role="listbox"]');
    expect(list.tabIndex).toBe(0);
    expect(all(container, '[role="option"]').every(row => row.tabIndex === -1)).toBe(true);
    await act(async () => list.focus());
    expect(document.activeElement).toBe(all(container, '[role="option"]')[0] as HTMLElement);
  });

  test('arrows, Home and End move the focus without wrapping; Enter and Space pick', async () => {
    const picked: string[] = [];
    const {container} = await mount(<Candidates selected="a" picked={picked} />);
    const rows = all(container, '[role="option"]');
    const focused = (): number => rows.indexOf(document.activeElement as HTMLElement);
    await press(rows[0] as HTMLElement, 'ArrowUp');
    expect(focused()).toBe(0);
    await press(rows[0] as HTMLElement, 'ArrowDown');
    expect(focused()).toBe(1);
    await press(rows[1] as HTMLElement, 'End');
    expect(focused()).toBe(2);
    await press(rows[2] as HTMLElement, 'ArrowDown');
    expect(focused()).toBe(2);
    await press(rows[2] as HTMLElement, 'Home');
    expect(focused()).toBe(0);
    await press(rows[1] as HTMLElement, 'Enter');
    await press(rows[2] as HTMLElement, ' ');
    await click(rows[0] as HTMLElement);
    expect(picked).toEqual(['b', 'c', 'a']);
  });

  test('a row shows its lineage guides, state, badges, bar and figure, and names its state', async () => {
    const {container} = await mount(<Candidates selected="a" />);
    const [a, , c] = all(container, '[role="option"]') as [HTMLElement, HTMLElement, HTMLElement];
    expect(all(a, '.vs-guide')).toHaveLength(0);
    expect(all(c, '.vs-guide').map(guide => guide.getAttribute('data-guide'))).toEqual([
      'blank',
      'elbow',
    ]);
    expect(one(c, '[role="img"][aria-label="State of c"]')).toBeDefined();
    expect(one(c, '.vs-pickrow-dot').getAttribute('data-tip')).toBe('State of c');
    expect(one(c, '.vs-chip').textContent).toBe('xc');
    expect(one(c, '[aria-label="Bar of c"]')).toBeDefined();
    expect(one(c, '.vs-pickrow-figure').textContent).toBe('1.0x');
  });

  test('the header names the columns and describes the list', async () => {
    const {container} = await mount(<Candidates selected="a" />);
    const header = one(container, '.vs-listheader');
    expect(header.textContent).toBe('candidatevs input');
    expect(one(container, '[role="listbox"]').getAttribute('aria-describedby')).toBe(header.id);
  });

  test('marks the path row, and reports the pointer or the focus entering and leaving a row', async () => {
    const hovered: string[] = [];
    const {container} = await mount(<Candidates selected="a" hovered={hovered} />);
    const rows = all(container, '[role="option"]');
    expect(rows.map(row => row.hasAttribute('data-path'))).toEqual([false, true, false]);
    await act(async () => {
      rows[0]?.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
      rows[0]?.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
    });
    await act(async () => rows[1]?.focus());
    await act(async () => rows[1]?.blur());
    expect(hovered).toContain('b:true');
    expect(hovered).toContain('b:false');
  });
});

describe('Meter', () => {
  test('keeps any value, however wrong, within the whole', async () => {
    for (const value of [Number.NEGATIVE_INFINITY, -3, 0, 0.25, 1, 7, Number.NaN]) {
      const {container} = await mount(<Meter label="Share" value={value} />);
      const share = Number.parseFloat(
        one(container, '.vs-meter').style.getPropertyValue('--meter-share'),
      );
      expect(share).toBeGreaterThanOrEqual(0);
      expect(share).toBeLessThanOrEqual(100);
      expect(one(container, '[aria-label="Share"]')).toBeDefined();
    }
  });
});

describe('NewBelow', () => {
  test('says what arrived and brings it into view when pressed', async () => {
    let pressed = 0;
    const {container} = await mount(
      <NewBelow
        text="3 new"
        press={() => {
          pressed += 1;
        }}
      />,
    );
    expect(one(container, 'button').textContent).toBe('3 new');
    await click(one(container, 'button'));
    expect(pressed).toBe(1);
  });
});

function row(id: string, over: Partial<GanttRow>): GanttRow {
  return {
    id,
    label: id,
    start: 0,
    end: 10,
    outcome: 'ok',
    segments: [],
    press: noop,
    ...over,
  };
}

describe('Gantt', () => {
  test('rows are options of a named listbox with one tab stop, and open by click and key', async () => {
    const opened: string[] = [];
    const rows = ['a', 'b'].map(id =>
      row(id, {press: () => opened.push(id), selected: id === 'b'}),
    );
    const {container} = await mount(<Gantt label="Over time" from={0} to={10} rows={rows} />);
    expect(one(container, '[role="listbox"]').getAttribute('aria-label')).toBe('Over time');
    const options = all(container, '[role="option"]');
    expect(options.map(option => option.tabIndex)).toEqual([-1, 0]);
    await press(options[1] as HTMLElement, 'ArrowUp');
    expect(document.activeElement).toBe(options[0] as HTMLElement);
    await press(options[0] as HTMLElement, 'Enter');
    await click(options[1] as HTMLElement);
    expect(opened).toEqual(['a', 'b']);
  });

  test('without a selection the first row is the tab stop', async () => {
    const rows = ['a', 'b'].map(id => row(id, {}));
    const {container} = await mount(<Gantt label="Over time" from={0} to={10} rows={rows} />);
    expect(all(container, '[role="option"]').map(option => option.tabIndex)).toEqual([0, -1]);
  });

  test('pressing a segment opens the step and not the row', async () => {
    const opened: string[] = [];
    const rows = [
      row('a', {
        press: () => opened.push('row'),
        segments: [
          {id: 's1', start: 0, end: 4, kind: 'solid', label: 'one', press: () => opened.push('s1')},
          {id: 's2', start: 4, end: 10, kind: 'hatched', label: 'two'},
        ],
      }),
    ];
    const {container} = await mount(<Gantt label="Over time" from={0} to={10} rows={rows} />);
    const segments = all(container, '.vs-gantt-seg');
    expect(segments.map(segment => segment.getAttribute('title'))).toEqual(['one', 'two']);
    expect(segments.map(segment => segment.getAttribute('data-kind'))).toEqual([
      'solid',
      'hatched',
    ]);
    await click(segments[0] as HTMLElement);
    await click(segments[1] as HTMLElement);
    expect(opened).toEqual(['s1', 'row']);
  });

  test('never gives a bar or a segment a negative length, whatever the times', async () => {
    const cases: readonly [number, number][] = [
      [-5, 3],
      [8, 40],
      [4, 4],
      [9, 2],
      [0, 10],
    ];
    const rows = cases.map(([start, end], index) =>
      row(`r${index}`, {
        start,
        end,
        segments: [{id: 's', start: start - 1, end: end + 1, kind: 'solid', label: 's'}],
      }),
    );
    const {container} = await mount(<Gantt label="Over time" from={0} to={10} rows={rows} />);
    for (const element of all(container, '.vs-gantt-bar, .vs-gantt-seg')) {
      const span = Number.parseFloat(
        element.style.getPropertyValue('--gantt-span') ||
          element.style.getPropertyValue('--seg-span'),
      );
      expect(span).toBeGreaterThanOrEqual(0);
    }
    for (const segment of all(container, '.vs-gantt-seg')) {
      const from = Number.parseFloat(segment.style.getPropertyValue('--seg-from'));
      const span = Number.parseFloat(segment.style.getPropertyValue('--seg-span'));
      expect(from).toBeGreaterThanOrEqual(0);
      expect(from + span).toBeLessThanOrEqual(100.01);
    }
  });

  test('draws ticks and column names on its axis row, and a playhead through every row', async () => {
    const {container} = await mount(
      <Gantt
        label="Over time"
        from={0}
        to={10}
        playhead={5}
        ticks={[
          {at: 0, label: '0m'},
          {at: 10, label: '10m'},
        ]}
        nameLabel="candidate"
        figureLabel="vs input"
        rows={['a', 'b'].map(id => row(id, {}))}
      />,
    );
    const axis = one(container, '[data-axis]');
    expect(axis.textContent).toBe('candidate0m10mvs input');
    expect(all(container, '.vs-gantt-playhead')).toHaveLength(2);
  });

  test('a strip has no names, ticks or axis row', async () => {
    const {container} = await mount(
      <Gantt
        label="Steps"
        from={0}
        to={10}
        strip
        ticks={[{at: 0, label: '0m'}]}
        rows={[row('a', {})]}
      />,
    );
    expect(container.querySelector('[data-axis]')).toBeNull();
    expect(container.querySelector('.vs-gantt-name')).toBeNull();
    expect(one(container, '[role="option"]').getAttribute('aria-label')).toBe('a');
  });
});

describe('Chart picks', () => {
  const points = [
    {id: 'a', label: 'A', x: 0, y: 1, selected: false},
    {id: 'b', label: 'B', x: 1, y: 2, selected: true},
    {x: 2, y: 3},
  ];

  test('a point with an id is a button that picks it, and the selected one says so', async () => {
    const picked: string[] = [];
    const {container} = await mount(
      <Chart label="Series" points={points} pick={id => picked.push(id)} />,
    );
    const buttons = all(container, '[role="button"]');
    expect(buttons.map(button => button.getAttribute('aria-label'))).toEqual(['A', 'B']);
    expect(buttons.map(button => button.getAttribute('aria-pressed'))).toEqual(['false', 'true']);
    await click(buttons[0] as HTMLElement);
    await press(buttons[1] as HTMLElement, 'Enter');
    expect(picked).toEqual(['a', 'b']);
    expect(all(container, '.vs-chart-ring')).toHaveLength(1);
    expect(all(container, '.vs-chart-point')).toHaveLength(3);
  });

  test('without a pick there are no buttons, and a selected point is still ringed', async () => {
    const {container} = await mount(<Chart label="Series" points={points} />);
    expect(container.querySelector('[role="button"]')).toBeNull();
    expect(one(container, 'svg').getAttribute('role')).toBe('img');
    expect(all(container, '.vs-chart-ring')).toHaveLength(1);
  });

  test('a strip is shorter than the default chart', async () => {
    const height = async (size?: 'strip'): Promise<number> => {
      const {container} = await mount(
        <Chart label="Series" points={points} {...(size === undefined ? {} : {size})} />,
      );
      return Number(one(container, 'svg').getAttribute('viewBox')?.split(' ')[3]);
    };
    expect(await height('strip')).toBeLessThan(await height());
  });
});

describe('Chart tip', () => {
  const points = [
    {id: 'a', label: 'Keep a free list · 8.7 ms · 1.43×', x: 0, y: 1},
    {id: 'b', label: 'Batch the enqueue · 4.9M ops/s', x: 1, y: 2},
  ];

  test('hovering a point says its label, and leaving it takes the words away', async () => {
    const {container} = await mount(<Chart label="Series" points={points} pick={noop} />);
    expect(container.querySelector('.vs-chart-tip')).toBeNull();
    const [first] = all(container, '[role="button"]') as [HTMLElement];
    await act(async () => {
      first.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
    });
    expect(one(container, '.vs-chart-tip-text').textContent).toBe(points[0]?.label as string);
    await act(async () => {
      first.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
    });
    expect(container.querySelector('.vs-chart-tip')).toBeNull();
  });
});

describe('Chart cursor', () => {
  const points = [
    {id: 'a', x: 1, y: 2},
    {id: 'b', x: 3, y: 3},
    {id: 'c', x: 5, y: 1},
  ];
  const steps = [
    {x: 0, y: 1},
    {x: 3, y: 3},
    {x: 6, y: 3},
  ];

  test('draws a line there, dims the dots after it and the line after it, and moves nothing', async () => {
    const plain = await mount(<Chart label="Series" points={points} steps={steps} />);
    const {container} = await mount(
      <Chart label="Series" points={points} steps={steps} cursor={4} />,
    );
    expect(plain.container.querySelector('.vs-chart-cursor')).toBeNull();
    expect(all(container, '.vs-chart-cursor')).toHaveLength(1);
    expect(all(container, '.vs-chart-point').map(dot => dot.hasAttribute('data-after'))).toEqual([
      false,
      false,
      true,
    ]);
    // The line is one path, drawn twice over the halves the cursor divides: the second dimmed.
    expect(all(container, 'path.vs-chart-step')).toHaveLength(2);
    expect(all(container, 'path.vs-chart-step[data-after]')).toHaveLength(1);
    const where = (each: HTMLElement) =>
      all(each, '.vs-chart-point').map(dot => [dot.getAttribute('cx'), dot.getAttribute('cy')]);
    expect(where(container)).toEqual(where(plain.container));
  });

  test('marks on the axis and a caption, with a sample of a mark and its word', async () => {
    const {container} = await mount(
      <Chart
        label="Series"
        points={points}
        marks={[2, 4]}
        markLabel="resumed"
        caption="running time · 25m"
      />,
    );
    expect(all(container, '.vs-chart-mark').length).toBe(3);
    const words = all(container, '.vs-chart-label').map(each => each.textContent);
    expect(words).toContain('running time · 25m');
    expect(words).toContain('resumed');
    const unmarked = await mount(
      <Chart label="Series" points={points} markLabel="resumed" caption="running time · 25m" />,
    );
    expect(all(unmarked.container, '.vs-chart-label').map(each => each.textContent)).not.toContain(
      'resumed',
    );
  });

  test('names what the line starts from, above it', async () => {
    const {container} = await mount(
      <Chart label="Series" points={points} steps={steps} start="Input" />,
    );
    expect(all(container, '.vs-chart-label').map(each => each.textContent)).toContain('Input');
  });
});

describe('Chart lit chain', () => {
  const points = [
    {id: 'a', x: 1, y: 2},
    {id: 'b', x: 2, y: 3},
    {id: 'c', x: 3, y: 1},
  ];

  test('rings the dots on the chain and draws no line between points', async () => {
    const {container} = await mount(
      <Chart
        label="Series"
        steps={[
          {x: 1, y: 2},
          {x: 2, y: 3},
        ]}
        points={points.map(point => ({...point, lit: point.id !== 'c'}))}
      />,
    );
    expect(all(container, '.vs-chart-lit')).toHaveLength(2);
    // The only drawn path is the best-so-far step line: nothing joins one dot to another.
    expect(all(container, 'path')).toHaveLength(1);
    expect(all(container, 'line:not([class])')).toHaveLength(0);
  });

  test('adds no point and moves no axis', async () => {
    const plain = await mount(<Chart label="Series" points={points} />);
    const drawn = await mount(
      <Chart label="Series" points={points.map(point => ({...point, lit: true}))} />,
    );
    const where = (container: HTMLElement) =>
      all(container, '.vs-chart-point').map(each => [
        each.getAttribute('cx'),
        each.getAttribute('cy'),
      ]);
    expect(where(drawn.container)).toEqual(where(plain.container));
  });

  test('tells the page which point the pointer or the focus is on, and when it leaves', async () => {
    const seen: (string | null)[] = [];
    const {container} = await mount(
      <Chart label="Series" points={points} pick={noop} hover={id => seen.push(id)} />,
    );
    const [first] = all(container, '[role="button"]') as [HTMLElement];
    await act(async () => {
      first.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
      first.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
    });
    expect(seen).toEqual(['a', null]);
  });
});

describe('Chart legend, panel and columns', () => {
  test('a legend names each mark as the chart draws it', async () => {
    const {container} = await mount(
      <ChartLegend
        label="Chart key"
        items={[
          {label: 'Best so far', mark: 'line'},
          {label: 'Reading', mark: 'dot'},
          {label: 'Selected', mark: 'ring'},
        ]}
      />,
    );
    expect(one(container, 'ul').getAttribute('aria-label')).toBe('Chart key');
    expect(all(container, 'li').map(item => item.textContent)).toEqual([
      'Best so far',
      'Reading',
      'Selected',
    ]);
    expect(
      all(container, 'li svg .vs-chart-step, li svg .vs-chart-point, li svg .vs-chart-ring'),
    ).toHaveLength(3);
  });

  test('a panel puts its figure at the far end of its heading, with its hint on hover', async () => {
    const {container} = await mount(
      <ChartPanel
        title="Throughput"
        note="higher is better"
        figure="3.83× faster"
        tone="ok"
        hint="6M · input 1.58M"
        unit="ops/s"
      >
        <span>chart</span>
      </ChartPanel>,
    );
    expect(one(container, 'section').getAttribute('aria-label')).toBe('Throughput');
    const figure = one(container, '.vs-chart-panel-head > .vs-text');
    expect(figure.textContent).toBe('3.83× faster');
    expect(figure.getAttribute('data-tone')).toBe('ok');
    expect(figure.getAttribute('title')).toBe('6M · input 1.58M');
    expect(container.textContent).toContain('ops/s');
  });

  test('columns hold their charts as children, in order', async () => {
    const {container} = await mount(
      <ChartColumns>
        <span>one</span>
        <span>two</span>
      </ChartColumns>,
    );
    expect(one(container, '.vs-chart-columns').textContent).toBe('onetwo');
  });
});

describe('fill size', () => {
  test('a log pane and a thread say they fill the pane they are in', async () => {
    const {container} = await mount(
      <>
        <LogPane label="Log" lines={[{text: 'x'}]} size="fill" />
        <Thread label="Thread" items={[]} size="fill" />
      </>,
    );
    expect(one(container, '.vs-log').getAttribute('data-size')).toBe('fill');
    expect(one(container, '.vs-thread').getAttribute('data-size')).toBe('fill');
  });
});

describe('DotLegend', () => {
  test('names each dot beside it', async () => {
    const {container} = await mount(
      <DotLegend
        items={[
          {tone: 'ok', label: '2 kept'},
          {tone: 'live', label: '1 running'},
        ]}
      />,
    );
    expect(container.textContent).toBe('2 kept1 running');
    expect(all(container, '[role="img"]').map(dot => dot.getAttribute('data-tone'))).toEqual([
      'ok',
      'live',
    ]);
  });
});

function Switching() {
  const [on, setOn] = useState(false);
  return (
    <PickList label="L" selected="x">
      <PickRow
        id="x"
        title="x"
        state={{tone: on ? 'ok' : 'err', label: on ? 'on' : 'off'}}
        select={() => setOn(!on)}
      />
    </PickList>
  );
}

describe('PickRow state', () => {
  test('follows the state it is given', async () => {
    const {container} = await mount(<Switching />);
    expect(one(container, '.vs-pickrow-dot').getAttribute('data-tip')).toBe('off');
    await click(one(container, '[role="option"]'));
    expect(one(container, '.vs-pickrow-dot').getAttribute('data-tip')).toBe('on');
  });
});

function tools(): ToolLine[] {
  return [
    {id: 'a', name: 'read', line: 'one.rs', state: 'ok', press: noop},
    {
      id: 'b',
      name: 'execute',
      line: 'cargo test',
      outcome: 'exit 101',
      state: 'failed',
      press: noop,
    },
  ];
}

function Group(props: {readonly pressed: string[]; readonly start: boolean}) {
  const [open, setOpen] = useState(props.start);
  const lines = tools().map(tool => ({...tool, press: () => props.pressed.push(tool.id)}));
  return <ToolGroup tools={lines} open={open} toggle={setOpen} />;
}

describe('Narrative', () => {
  test('keeps thinking, text and tool lines in the order given', async () => {
    const {container} = await mount(
      <Narrative label="Turn">
        <Thought text="first" />
        <Said text="second" />
        <ToolGroup tools={tools()} open={false} toggle={noop} />
      </Narrative>,
    );
    expect(one(container, 'section').getAttribute('aria-label')).toBe('Turn');
    expect(container.textContent?.startsWith('firstsecond2 tools')).toBe(true);
  });

  test('a closed group is one line that still shows a failed call, and opens to every call', async () => {
    const pressed: string[] = [];
    const {container} = await mount(<Group pressed={pressed} start={false} />);
    const head = one(container, '.vs-toolgroup-head');
    expect(head.getAttribute('aria-expanded')).toBe('false');
    expect(head.textContent).toContain('2 tools');
    expect(all(container, '.vs-toolgroup-row').map(r => r.getAttribute('data-state'))).toEqual([
      'failed',
    ]);
    await click(head);
    expect(head.getAttribute('aria-expanded')).toBe('true');
    expect(all(container, '.vs-toolgroup-row')).toHaveLength(2);
    await click(all(container, '.vs-toolgroup-row')[0] as HTMLElement);
    expect(pressed).toEqual(['a']);
  });

  test('a turn that follows its end offers "New below" once scrolled up, and jumps back', async () => {
    const {container} = await mount(
      <Narrative label="Turn" start="end">
        <Said key="a" text="a" />
        <Said key="b" text="b" />
      </Narrative>,
    );
    expect(container.querySelector('.vs-new-below')).toBeNull();
    const scroller = one(container, 'section');
    for (const [name, value] of [
      ['scrollHeight', 1000],
      ['clientHeight', 100],
      ['scrollTop', 0],
    ] as const) {
      Object.defineProperty(scroller, name, {configurable: true, writable: true, value});
    }
    await act(async () => {
      scroller.dispatchEvent(new UIEvent('scroll', {bubbles: true}));
    });
    await click(one(container, '.vs-new-below'));
    expect(container.querySelector('.vs-new-below')).toBeNull();
    expect(scroller.scrollTop).toBe(1000);
  });

  test('a reader who scrolled up keeps the place when the turn updates before the scroll event arrives', async () => {
    // A scroll event is delivered a frame after the scroll; an update may land in between.
    const view = (count: number) => (
      <Narrative label="Turn" start="end">
        {Array.from({length: count}, (_, index) => (
          <Said key={String(index)} text={`block ${index}`} />
        ))}
      </Narrative>
    );
    const {container, render} = await mount(view(3));
    const scroller = one(container, 'section');
    let height = 1000;
    let top = 0;
    const clamp = (value: number): number => Math.min(Math.max(value, 0), height - 100);
    Object.defineProperties(scroller, {
      scrollHeight: {configurable: true, get: () => height},
      clientHeight: {configurable: true, value: 100},
      scrollTop: {configurable: true, get: () => top, set: (value: number) => (top = clamp(value))},
    });
    await render(view(3));
    expect(top).toBe(900);

    top = 400;
    height = 1200;
    await render(view(4));
    expect(top).toBe(400);
    await act(async () => {
      scroller.dispatchEvent(new UIEvent('scroll', {bubbles: true}));
    });
    expect(top).toBe(400);
    expect(container.querySelector('.vs-new-below')).not.toBeNull();
  });

  test('a turn that does not follow its end never offers it', async () => {
    const {container} = await mount(
      <Narrative label="Turn">
        <Said key="a" text="a" />
      </Narrative>,
    );
    const scroller = one(container, 'section');
    Object.defineProperty(scroller, 'scrollHeight', {configurable: true, value: 1000});
    await act(async () => {
      scroller.dispatchEvent(new UIEvent('scroll', {bubbles: true}));
    });
    expect(container.querySelector('.vs-new-below')).toBeNull();
  });

  test('draws a title in bold and keeps paragraphs and code marks, never as markup', async () => {
    const {container} = await mount(
      <Narrative label="Turn">
        <Thought text={'**Inspecting history**\n\nRead `git log` first.'} />
        <Said text="Use *both* <b>counters</b>." />
      </Narrative>,
    );
    expect(one(container, '.vs-thought strong').textContent).toBe('Inspecting history');
    expect(all(container, '.vs-thought p')).toHaveLength(2);
    expect(one(container, '.vs-thought code').textContent).toBe('git log');
    expect(container.textContent).not.toContain('**');
    expect(one(container, '.vs-said em').textContent).toBe('both');
    expect(container.querySelector('.vs-said b')).toBeNull();
    expect(one(container, '.vs-said').textContent).toContain('<b>counters</b>');
  });

  test('says "1 tool" for one and marks the selected call', async () => {
    const [first] = tools() as [ToolLine];
    const {container} = await mount(
      <ToolGroup tools={[{...first, selected: true}]} open toggle={noop} />,
    );
    expect(one(container, '.vs-toolgroup-count').textContent).toBe('1 tool');
    expect(one(container, '.vs-toolgroup-row').getAttribute('aria-current')).toBe('true');
  });
});

describe('PickRow extras', () => {
  test('shows its trailing column, a link to its parent, and an accent stripe when on the chain', async () => {
    const {container} = await mount(
      <PickList label="Ideas" selected="a">
        <PickRow id="a" title="a" trail={{label: 'from input'}} stripe select={noop} />
        <PickRow id="b" title="b" select={noop} />
      </PickList>,
    );
    const [a, b] = all(container, '[role="option"]') as [HTMLElement, HTMLElement];
    expect(one(a, '.vs-pickrow-trail').textContent).toBe('from input');
    expect(a.hasAttribute('data-stripe')).toBe(true);
    expect(b.hasAttribute('data-stripe')).toBe(false);
    expect(b.querySelector('.vs-pickrow-trail')).toBeNull();
    const opened: string[] = [];
    const linked = await mount(
      <PickList label="Ideas" selected="a">
        <PickRow
          id="a"
          title="a"
          trail={{label: 'from input', press: () => opened.push('parent')}}
          select={() => opened.push('row')}
        />
      </PickList>,
    );
    await click(one(linked.container, 'button.vs-pickrow-trail'));
    expect(opened).toEqual(['parent']);
  });
});

describe('Chart lit chain', () => {
  const points = [
    {id: 'a', x: 1, y: 2, filled: true},
    {id: 'b', x: 2, y: 3, filled: true},
    {id: 'c', x: 3, y: 1, filled: true},
  ];

  test('rings the dots on the chain and draws no line between points', async () => {
    const {container} = await mount(
      <Chart
        label="Series"
        steps={[
          {x: 1, y: 2},
          {x: 2, y: 3},
        ]}
        points={points.map(point => ({...point, lit: point.id !== 'c'}))}
      />,
    );
    expect(all(container, '.vs-chart-lit')).toHaveLength(2);
    // The only drawn path is the best-so-far step line: nothing joins one dot to another.
    expect(all(container, 'path')).toHaveLength(1);
    expect(all(container, 'line:not([class])')).toHaveLength(0);
    expect(all(container, '.vs-chart-edge')).toHaveLength(0);
  });

  test('adds no point and moves no axis', async () => {
    const plain = await mount(<Chart label="Series" points={points} />);
    const drawn = await mount(
      <Chart label="Series" points={points.map(point => ({...point, lit: true}))} />,
    );
    const where = (container: HTMLElement) =>
      all(container, '.vs-chart-point').map(each => [
        each.getAttribute('cx'),
        each.getAttribute('cy'),
      ]);
    expect(where(drawn.container)).toEqual(where(plain.container));
  });

  test('tells the page which point the pointer or the focus is on, and when it leaves', async () => {
    const seen: (string | null)[] = [];
    const {container} = await mount(
      <Chart label="Series" points={points} pick={noop} hover={id => seen.push(id)} />,
    );
    const [first] = all(container, '[role="button"]') as [HTMLElement];
    await act(async () => {
      first.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
      first.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
    });
    expect(seen).toEqual(['a', null]);
  });
});

describe('PickRow fold', () => {
  test('a foldable row has a chevron that folds and unfolds it, by click and by Left and Right', async () => {
    const seen: string[] = [];
    const {container} = await mount(
      <PickList label="Ideas">
        <PickRow
          id="a"
          title="a"
          fold={{open: false, toggle: () => seen.push('a')}}
          select={() => seen.push('pick')}
        />
        <PickRow id="b" title="b" fold={{open: true, toggle: () => seen.push('b')}} select={noop} />
        <PickRow id="c" title="c" fold="none" select={noop} />
        <PickRow id="d" title="d" select={noop} />
      </PickList>,
    );
    const [a, b, c, d] = all(container, '[role="option"]') as [
      HTMLElement,
      HTMLElement,
      HTMLElement,
      HTMLElement,
    ];
    expect(one(a, '.vs-pickrow-fold').getAttribute('data-fold')).toBe('closed');
    expect(one(b, '.vs-pickrow-fold').getAttribute('data-fold')).toBe('open');
    // A row with nothing to fold keeps the slot so titles line up; a row of another list has none.
    expect(c.querySelector('.vs-pickrow-fold')).not.toBeNull();
    expect(c.querySelector('.vs-pickrow-fold')?.hasAttribute('data-fold')).toBe(false);
    expect(d.querySelector('.vs-pickrow-fold')).toBeNull();
    // The chevron holds no control of its own, so the option holds none (nothing nested).
    expect(a.querySelector('button')).toBeNull();
    await click(one(a, '.vs-pickrow-fold'));
    expect(seen).toEqual(['a']);
    await click(one(a, '.vs-pickrow-text'));
    expect(seen).toEqual(['a', 'pick']);
    seen.length = 0;
    await click(one(c, '.vs-pickrow-fold'));
    expect(seen).toEqual([]);
    seen.push('a');
    await press(a, 'ArrowRight');
    await press(a, 'ArrowLeft');
    await press(b, 'ArrowLeft');
    await press(b, 'ArrowRight');
    expect(seen).toEqual(['a', 'a', 'b']);
  });
});

describe('Breadcrumb figure', () => {
  test('names a chain with its result after the last level', async () => {
    const {container} = await mount(
      <Breadcrumb
        label="Best path"
        items={[
          {id: 'input', label: 'input', press: noop},
          {id: 'a', label: 'a'},
        ]}
        figure="5.72x"
      />,
    );
    expect(one(container, '.vs-crumb-figure').textContent).toBe('5.72x');
  });
});

const MANY = 10_000;
/** The most rows of a windowed list that may be in the DOM at once, far fewer than `MANY`. */
const DRAWN_AT_MOST = 120;

describe('at scale: windowed lists draw a bounded number of rows', () => {
  test('PickList: 10k rows, and the keys reach the last row', async () => {
    const picked: string[] = [];
    const ids = Array.from({length: MANY}, (_, index) => `r${index}`);
    const {container} = await mount(
      <PickList label="Ideas" selected="r0">
        {ids.map(id => (
          <PickRow key={id} id={id} title={id} select={() => picked.push(id)} />
        ))}
      </PickList>,
    );
    const options = (): HTMLElement[] => all(container, '[role="option"]');
    expect(options().length).toBeGreaterThan(0);
    expect(options().length).toBeLessThan(DRAWN_AT_MOST);
    expect(options()[0]?.getAttribute('aria-setsize')).toBe(String(MANY));
    await press(options()[0] as HTMLElement, 'End');
    const last = document.activeElement as HTMLElement;
    expect(last.getAttribute('aria-posinset')).toBe(String(MANY));
    expect(options().length).toBeLessThan(DRAWN_AT_MOST);
    await press(last, 'Enter');
    expect(picked).toEqual(['r9999']);
    await press(last, 'Home');
    expect((document.activeElement as HTMLElement).getAttribute('aria-posinset')).toBe('1');
  });

  test('PickList: the drawn rows stay bounded at 600, 10k and 100k, also after scrolling to the end', async () => {
    const sizes: number[] = [];
    for (const count of [600, MANY, 10 * MANY]) {
      const ids = Array.from({length: count}, (_, index) => `r${index}`);
      const {container} = await mount(
        <PickList label="Ideas" selected="r0">
          {ids.map(id => (
            <PickRow key={id} id={id} title={id} select={noop} />
          ))}
        </PickList>,
      );
      const first = one(container, '[role="option"]');
      sizes.push(all(container, '[role="option"]').length);
      await press(first, 'End');
      sizes.push(all(container, '[role="option"]').length);
      expect((document.activeElement as HTMLElement).getAttribute('aria-posinset')).toBe(
        String(count),
      );
    }
    for (const size of sizes) expect(size).toBeLessThan(DRAWN_AT_MOST);
    expect(new Set(sizes).size).toBeLessThanOrEqual(2);
  });

  test('PickList: arrows and pages step across the drawn window to any row', async () => {
    const ids = Array.from({length: MANY}, (_, index) => `r${index}`);
    const {container} = await mount(
      <PickList label="Ideas" selected="r0">
        {ids.map(id => (
          <PickRow key={id} id={id} title={id} select={noop} />
        ))}
      </PickList>,
    );
    let at = 1;
    for (const key of ['ArrowDown', 'ArrowDown', 'PageDown', 'ArrowUp']) {
      const focused = (document.activeElement as HTMLElement).closest('[role="option"]');
      const row = (focused ?? one(container, '[role="option"]')) as HTMLElement;
      await press(row, key);
      at = Number((document.activeElement as HTMLElement).getAttribute('aria-posinset'));
    }
    expect(at).toBeGreaterThan(3);
  });

  test('Thread: 10k entries', async () => {
    const items: ThreadItem[] = Array.from({length: MANY}, (_, index) => ({
      kind: 'message',
      id: `m${index}`,
      author: 'Worker',
      variant: 'note',
      text: `entry ${index}`,
    }));
    const {container} = await mount(<Thread label="Thread" items={items} />);
    expect(all(container, 'li.vs-thread-row').length).toBeLessThan(DRAWN_AT_MOST);
    expect(all(container, 'li.vs-thread-row').length).toBeGreaterThan(0);
  });

  test('LogPane: 50k lines, the tail drawn', async () => {
    const lines = Array.from({length: 5 * MANY}, (_, index) => ({text: `line ${index}`}));
    const {container} = await mount(<LogPane label="Log" lines={lines} />);
    const drawn = all(container, '.vs-log-line');
    expect(drawn.length).toBeLessThan(DRAWN_AT_MOST);
    expect(drawn.at(-1)?.textContent).toBe('line 49999');
  });

  test('LogPane: 500k lines are as cheap as 50k, and it opens at the tail', async () => {
    const counts: number[] = [];
    for (const length of [5 * MANY, 50 * MANY]) {
      const lines = Array.from({length}, (_, index) => ({text: `line ${index}`}));
      const {container} = await mount(<LogPane label="Log" lines={lines} />);
      const drawn = all(container, '.vs-log-line');
      counts.push(drawn.length);
      expect(drawn.at(-1)?.textContent).toBe(`line ${length - 1}`);
    }
    expect(counts[0]).toBe(counts[1] as number);
  });

  test('LogPane: start="top" opens at the first line of 50k', async () => {
    const lines = Array.from({length: 5 * MANY}, (_, index) => ({text: `line ${index}`}));
    const {container} = await mount(<LogPane label="Log" lines={lines} start="top" />);
    const drawn = all(container, '.vs-log-line');
    expect(drawn.length).toBeLessThan(DRAWN_AT_MOST);
    expect(drawn.at(0)?.textContent).toBe('line 0');
  });

  test('Narrative: 10k blocks', async () => {
    const blocks = Array.from({length: MANY}, (_, index) => ({
      id: `b${index}`,
      said: index % 2 === 0,
    }));
    const {container} = await mount(
      <Narrative label="Turn">
        {blocks.map(block =>
          block.said ? (
            <Said key={block.id} text={block.id} />
          ) : (
            <Thought key={block.id} text={block.id} />
          ),
        )}
      </Narrative>,
    );
    const drawn = all(container, '.vs-said, .vs-thought');
    expect(drawn.length).toBeGreaterThan(0);
    expect(drawn.length).toBeLessThan(DRAWN_AT_MOST);
  });

  test('Gantt: 10k rows, and the keys reach the last row', async () => {
    const opened: string[] = [];
    const rows = Array.from({length: MANY}, (_, index) =>
      row(`r${index}`, {press: () => opened.push(`r${index}`)}),
    );
    const {container} = await mount(<Gantt label="Over time" from={0} to={10} rows={rows} />);
    const options = (): HTMLElement[] => all(container, '[role="option"]');
    expect(options().length).toBeLessThan(DRAWN_AT_MOST);
    await press(options()[0] as HTMLElement, 'End');
    const last = document.activeElement as HTMLElement;
    expect(last.getAttribute('aria-posinset')).toBe(String(MANY));
    await press(last, 'Enter');
    expect(opened).toEqual(['r9999']);
  });

  test('Gantt: many rows read as bars without pieces unless asked for the detail', async () => {
    const piece = [{id: 's', start: 0, end: 5, kind: 'solid' as const, label: 's'}];
    const rows = Array.from({length: 200}, (_, index) => row(`r${index}`, {segments: piece}));
    const aggregated = await mount(<Gantt label="A" from={0} to={10} rows={rows} />);
    expect(all(aggregated.container, '.vs-gantt-seg')).toHaveLength(0);
    const detailed = await mount(<Gantt label="B" from={0} to={10} rows={rows} detail="full" />);
    expect(all(detailed.container, '.vs-gantt-seg').length).toBeGreaterThan(0);
  });

  test('Chart: thousands of points are binned to a bounded number of dots, the kept ones all drawn', async () => {
    const points = Array.from({length: 5000}, (_, index) => ({
      x: index,
      y: (index * 37) % 101,
      selected: index % 250 === 0,
    }));
    const {container} = await mount(<Chart label="Series" points={points} />);
    expect(all(container, '.vs-chart-ring')).toHaveLength(20);
    expect(all(container, '.vs-chart-point').length).toBeLessThanOrEqual(3000 + 20);
    const few = await mount(<Chart label="Few" points={points.slice(0, 500)} />);
    expect(all(few.container, '.vs-chart-point')).toHaveLength(500);
  });

  test('Chart: 600 points, small dots, one tab stop and arrows among the pickable', async () => {
    const points = Array.from({length: 600}, (_, index) => ({
      id: `p${index}`,
      x: index,
      y: (index * 7) % 50,
    }));
    const {container} = await mount(<Chart label="Series" points={points} pick={noop} />);
    const buttons = all(container, '[role="button"]');
    expect(buttons).toHaveLength(600);
    expect(buttons.filter(button => button.getAttribute('tabindex') === '0')).toHaveLength(1);
    const radii = new Set(all(container, '.vs-chart-point').map(dot => dot.getAttribute('r')));
    expect(radii).toEqual(new Set(['2.5']));
    await press(buttons[0] as unknown as HTMLElement, 'ArrowRight');
    expect(document.activeElement).toBe(buttons[1] as unknown as Element);
    await press(buttons[1] as unknown as HTMLElement, 'End');
    expect(document.activeElement).toBe(buttons[599] as unknown as Element);
  });
});

function Zoomable() {
  const [view, setView] = useState({from: 0, to: 100});
  return (
    <>
      <Gantt
        label="Over time"
        from={0}
        to={100}
        view={view}
        changeView={setView}
        rows={[row('a', {start: 0, end: 100})]}
      />
      <output>{`${view.from}-${view.to}`}</output>
    </>
  );
}

describe('Gantt zoom and pan', () => {
  const state = (container: HTMLElement): string => one(container, 'output').textContent ?? '';
  const named = (container: HTMLElement, name: string): HTMLElement => {
    const found = all(container, 'button').find(candidate => candidate.textContent === name);
    if (found === undefined) throw new Error(`no ${name}`);
    return found;
  };

  test('the buttons zoom about the middle, pan, and return to the whole range, never leaving it', async () => {
    const {container} = await mount(<Zoomable />);
    await click(named(container, 'Zoom in'));
    expect(state(container)).toBe('25-75');
    await click(named(container, 'Later'));
    expect(state(container)).toBe('37.5-87.5');
    for (let step = 0; step < 6; step++) await click(named(container, 'Later'));
    expect(state(container)).toBe('50-100');
    for (let step = 0; step < 6; step++) await click(named(container, 'Zoom in'));
    const [from, to] = state(container).split('-').map(Number) as [number, number];
    expect(to - from).toBeGreaterThanOrEqual(1);
    await click(named(container, 'Whole range'));
    expect(state(container)).toBe('0-100');
    await click(named(container, 'Zoom out'));
    expect(state(container)).toBe('0-100');
  });

  test('the keys do the same from the rows', async () => {
    const {container} = await mount(<Zoomable />);
    const rowEl = one(container, '[role="option"]');
    await press(rowEl, '+');
    expect(state(container)).toBe('25-75');
    await press(rowEl, 'ArrowRight');
    expect(state(container)).toBe('37.5-87.5');
    await press(rowEl, '0');
    expect(state(container)).toBe('0-100');
  });

  test('a bar is clipped by the view: it starts before it when the view is zoomed in', async () => {
    const {container} = await mount(
      <Gantt
        label="Over time"
        from={0}
        to={100}
        view={{from: 50, to: 100}}
        rows={[row('a', {start: 0, end: 100})]}
      />,
    );
    expect(one(container, '.vs-gantt-bar').style.getPropertyValue('--gantt-from')).toBe('-100.00%');
  });
});

function Filters() {
  const [on, setOn] = useState(false);
  const [text, setText] = useState('');
  return (
    <>
      <DotLegend
        items={[
          {tone: 'ok', label: '2 kept'},
          {tone: 'err', label: '5 rejected', on, toggle: setOn},
        ]}
      />
      <SearchBox label="Search ideas" value={text} change={setText} placeholder="Search" />
      <output>{`${on}:${text}`}</output>
    </>
  );
}

describe('filter chips and search', () => {
  test('a legend item with a toggle is a pressed-state button; one without stays text', async () => {
    const {container} = await mount(<Filters />);
    const chips = all(container, 'button.vs-dotlegend-item');
    expect(chips).toHaveLength(1);
    expect(chips[0]?.getAttribute('aria-pressed')).toBe('false');
    await click(chips[0] as HTMLElement);
    expect(chips[0]?.getAttribute('aria-pressed')).toBe('true');
    expect(one(container, 'output').textContent).toBe('true:');
    expect(all(container, 'span.vs-dotlegend-item')).toHaveLength(1);
  });

  test('the search box is a named search field showing the text it is given', async () => {
    const {container} = await mount(
      <SearchBox label="Search ideas" value="seqlock" change={noop} placeholder="Search" />,
    );
    const input = one(container, 'input[type="search"]') as unknown as HTMLInputElement;
    expect(input.getAttribute('aria-label')).toBe('Search ideas');
    expect(input.getAttribute('placeholder')).toBe('Search');
    expect(input.value).toBe('seqlock');
  });
});

describe('series colors', () => {
  test('Chart: a fixed y range keeps a value where it is whatever else is drawn', async () => {
    const a = {id: 'a', x: 1, y: 50};
    const b = {id: 'b', x: 2, y: 10};
    const domain = [0, 100] as const;
    const alone = await mount(<Chart label="One" points={[a]} yDomain={domain} />);
    const both = await mount(<Chart label="Two" points={[a, b]} yDomain={domain} />);
    expect(one(alone.container, 'circle').getAttribute('cy')).toBe(
      one(both.container, 'circle').getAttribute('cy'),
    );
  });

  test('Gantt: a segment with a series is drawn in that palette color, one without in none', async () => {
    const rows = [
      row('a', {
        segments: [
          {id: 's1', start: 0, end: 4, kind: 'solid', series: 3, label: 'one'},
          {id: 's2', start: 4, end: 10, kind: 'hatched', label: 'two'},
        ],
      }),
    ];
    const {container} = await mount(<Gantt label="Over time" from={0} to={10} rows={rows} />);
    const colors = all(container, '.vs-gantt-seg').map(seg =>
      seg.style.getPropertyValue('--seg-color'),
    );
    expect(colors).toEqual(['var(--series-3)', '']);
  });

  test('SwatchLegend: names each entry, hatched or in its palette color', async () => {
    const {container} = await mount(
      <SwatchLegend
        label="Bar colors"
        items={[
          {label: 'judge', series: 3},
          {label: 'gate', hatched: true},
        ]}
      />,
    );
    const items = all(container, 'li');
    expect(items.map(item => item.textContent)).toEqual(['judge', 'gate']);
    expect(one(container, '[aria-label="Bar colors"]').tagName).toBe('UL');
    expect(items.map(item => one(item, '.vs-series-swatch').hasAttribute('data-hatched'))).toEqual([
      false,
      true,
    ]);
    expect(
      one(items[0] as HTMLElement, '.vs-series-swatch').style.getPropertyValue('--seg-color'),
    ).toBe('var(--series-3)');
  });
});

describe('metric tiles', () => {
  test('a tile names its measure, its figure and what it means, and its quiet values with a title', async () => {
    const {container} = await mount(
      <MetricTiles label="Performance" count={2}>
        <MetricTile
          name="Throughput"
          figure="3.83×"
          note="faster than input"
          tone="ok"
          detail="6,054,154 ops/s · input 1,581,539 ops/s"
          trend={
            <Sparkline
              label="Best so far"
              steps={[
                {x: 0, y: 1},
                {x: 5, y: 3},
              ]}
              from={0}
              to={10}
              reference={1}
            />
          }
        />
        <MetricTile name="Latency" figure="–" />
      </MetricTiles>,
    );
    expect(all(container, '.vs-tile')).toHaveLength(2);
    expect(one(container, '.vs-tiles').getAttribute('aria-label')).toBe('Performance');
    expect(one(container, '.vs-tile-detail').getAttribute('title')).toBe(
      '6,054,154 ops/s · input 1,581,539 ops/s',
    );
    expect(one(container, '.vs-tile').textContent).toContain('faster than input');
    // The sparkline is one step line and a dashed reference, both inside its box.
    expect(all(container, 'svg.vs-spark path.vs-spark-line')).toHaveLength(1);
    expect(all(container, 'svg.vs-spark .vs-spark-reference')).toHaveLength(1);
  });

  test('a row of measures shows each column of a list row beside the figure', async () => {
    const {container} = await mount(
      <PickList label="Rows">
        <PickRow
          id="a"
          title="A"
          measure={[
            {label: '7.1M', note: '6.2M cached', size: 'note'},
            {label: '0.5M', size: 'plain'},
          ]}
          select={() => undefined}
        />
      </PickList>,
    );
    const cells = all(container, '.vs-pickrow-measure');
    expect(cells.map(cell => cell.textContent)).toEqual(['7.1M 6.2M cached', '0.5M']);
    expect(cells.map(cell => cell.getAttribute('data-size'))).toEqual(['note', 'plain']);
  });
});
