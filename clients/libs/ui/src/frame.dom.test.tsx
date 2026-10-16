/** The frame components (screen, panes, pick lists, meter, gantt, chart picks) in a DOM, through `.` only. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type ReactNode, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {
  Breadcrumb,
  Chart,
  DotLegend,
  Gantt,
  type GanttRow,
  LogPane,
  Meter,
  NewBelow,
  Pane,
  PickList,
  PickRow,
  Screen,
  Thread,
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
  });
});

describe('Breadcrumb', () => {
  test('links every level but the last, which names where the reader is', async () => {
    const visited: string[] = [];
    const {container} = await mount(
      <Breadcrumb
        label="Zoom"
        items={['run', 'candidate', 'step'].map((id, index) => ({
          id,
          label: id,
          ...(index === 2 ? {} : {press: () => visited.push(id)}),
        }))}
      />,
    );
    expect(one(container, 'nav').getAttribute('aria-label')).toBe('Zoom');
    const links = all(container, 'button');
    expect(links.map(link => link.textContent)).toEqual(['run', 'candidate']);
    await click(links[1] as HTMLElement);
    expect(visited).toEqual(['candidate']);
    expect(one(container, '[aria-current="location"]').textContent).toBe('step');
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
          {id: 's1', start: 0, end: 4, kind: 'agent', label: 'one', press: () => opened.push('s1')},
          {id: 's2', start: 4, end: 10, kind: 'gate', label: 'two'},
        ],
      }),
    ];
    const {container} = await mount(<Gantt label="Over time" from={0} to={10} rows={rows} />);
    const segments = all(container, '.vs-gantt-seg');
    expect(segments.map(segment => segment.getAttribute('title'))).toEqual(['one', 'two']);
    expect(segments.map(segment => segment.getAttribute('data-kind'))).toEqual(['agent', 'gate']);
    await click(segments[0] as HTMLElement);
    await click(segments[1] as HTMLElement);
    expect(opened).toEqual(['s1', 'row']);
  });

  test('keeps every bar and segment inside the axis, whatever the times', async () => {
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
        segments: [{id: 's', start: start - 1, end: end + 1, kind: 'agent', label: 's'}],
      }),
    );
    const {container} = await mount(<Gantt label="Over time" from={0} to={10} rows={rows} />);
    for (const element of all(container, '.vs-gantt-bar, .vs-gantt-seg')) {
      const from = Number.parseFloat(
        element.style.getPropertyValue('--gantt-from') ||
          element.style.getPropertyValue('--seg-from'),
      );
      const span = Number.parseFloat(
        element.style.getPropertyValue('--gantt-span') ||
          element.style.getPropertyValue('--seg-span'),
      );
      expect(from).toBeGreaterThanOrEqual(0);
      expect(span).toBeGreaterThanOrEqual(0);
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
    {id: 'a', label: 'A', x: 0, y: 1, filled: true},
    {id: 'b', label: 'B', x: 1, y: 2, filled: false},
    {x: 2, y: 3, filled: true},
  ];

  test('a point with an id is a button that picks it, and the selected one says so', async () => {
    const picked: string[] = [];
    const {container} = await mount(
      <Chart label="Series" points={points} selected="b" pick={id => picked.push(id)} />,
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
    const {container} = await mount(<Chart label="Series" points={points} selected="a" />);
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
