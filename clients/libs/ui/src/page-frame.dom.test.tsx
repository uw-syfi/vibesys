/**
 * The page frame every destination of a window is drawn in: the trail band above the title, the
 * title's own row (its heading, actions and meta), and the pieces the pages of a run are made of:
 * cards with their link in the heading, tiles on the charts' tracks, a split that keeps its main
 * column first, bars that never grow wider than a bar, and the key to a Gantt's bars.
 */
import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {
  Button,
  Card,
  ChartColumns,
  Gantt,
  Menu,
  MetricTile,
  MetricTiles,
  Page,
  Reserve,
  Split,
  StackedBars,
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

function one(container: HTMLElement, selector: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(selector);
  if (element === null) throw new Error(`no ${selector}`);
  return element;
}

const css = (name: string): string => readFileSync(new URL(`./${name}`, import.meta.url), 'utf8');

describe('Reserved layout room', () => {
  test('can give its invisible room back in the app narrow-window layout', async () => {
    const container = await mount(
      <Reserve collapseInNarrowWindow>
        <button type="button">Late action</button>
      </Reserve>,
    );
    const reserve = one(container, '.vs-reserve');
    expect(reserve.getAttribute('aria-hidden')).toBe('true');
    expect(reserve.hasAttribute('inert')).toBe(true);
    expect(reserve.getAttribute('data-collapse-narrow')).toBe('true');
    expect(css('base.css')).toMatch(
      /@media \(max-width: 799px\)[^@]*\.vs-reserve\[data-collapse-narrow\]\s*\{[^}]*display:\s*none/,
    );
  });
});

describe('Page with a title', () => {
  test('draws the trail band, then the title row with its heading, actions and meta', async () => {
    const container = await mount(
      <Page
        state="ready"
        title="Cost"
        trail={[
          {id: 'runs', label: 'Runs', press: () => undefined},
          {id: 'cost', label: 'Cost'},
        ]}
        actions={<Button text="New" press={() => undefined} />}
        meta={<span>Live</span>}
      >
        Body
      </Page>,
    );
    const header = one(container, '.vs-page-header');
    expect([...header.children].map(child => child.className)).toEqual([
      'vs-page-trail',
      'vs-page-title-row',
      'vs-page-meta',
    ]);
    const heading = one(header, 'h1');
    expect(heading.textContent).toBe('Cost');
    // The heading takes the focus when its page opens, which only a programmatic focus can do.
    expect(heading.getAttribute('tabindex')).toBe('-1');
    expect(one(header, '.vs-page-title-row').contains(one(header, '.vs-page-actions'))).toBe(true);
    expect([...header.querySelectorAll('.vs-crumb')].map(each => each.textContent)).toEqual([
      'Runs',
      'Cost',
    ]);
    expect(one(container, 'main').getAttribute('data-state')).toBe('ready');
  });

  test("has no trail band without a trail, so the title is the header's first row", async () => {
    const titled = await mount(
      <Page state="ready" title="Settings">
        Body
      </Page>,
    );
    const own = await mount(
      <Page state="ready" header={<h1>Runs</h1>}>
        Body
      </Page>,
    );
    const empty = await mount(
      <Page state="ready" title="Machines" trail={[]}>
        Body
      </Page>,
    );
    for (const container of [titled, own, empty]) {
      const header = one(container, '.vs-page-header');
      expect(header.querySelector('.vs-page-trail')).toBeNull();
      expect(header.querySelector('h1')).not.toBeNull();
    }
    expect(one(titled, '.vs-page-header').firstElementChild?.className).toBe('vs-page-title-row');
    // A page with a trail keeps its band as tall as ever, so it never moves when the crumbs load.
    expect(css('base.css')).toMatch(/\.vs-page-trail \{[^}]*min-height: var\(--row-dense\)/);
    // And the title row is a control tall, so a title with a button beside it is where one without is.
    expect(css('base.css')).toMatch(/\.vs-page-title-row \{[^}]*min-height: var\(--control\)/);
  });
});

describe('a button with words', () => {
  test('keeps its padding when it is also given a name, and only an icon alone is a square', async () => {
    const container = await mount(
      <>
        <Button text="Stop run" label="Stop run" press={() => undefined} />
        <Button text="Agents" press={() => undefined} />
        <Button icon="plus" label="New" press={() => undefined} />
      </>,
    );
    const [named, plain, icon] = container.querySelectorAll('.vs-button');
    expect(named?.hasAttribute('data-icon-only')).toBe(false);
    expect(plain?.hasAttribute('data-icon-only')).toBe(false);
    expect(icon?.hasAttribute('data-icon-only')).toBe(true);
    // A menu's trigger is a button too: an icon alone is a square, with words it is not.
    const menus = await mount(
      <>
        <Menu icon="more" label="More" items={[{id: 'a', label: 'A', press: () => undefined}]} />
        <Menu text="Actions" items={[{id: 'a', label: 'A', press: () => undefined}]} />
      </>,
    );
    const [more, actions] = menus.querySelectorAll('.vs-button');
    expect(more?.hasAttribute('data-icon-only')).toBe(true);
    expect(actions?.hasAttribute('data-icon-only')).toBe(false);
    // The square is for what is marked, not for whatever carries an aria-label.
    expect(css('inputs.css')).toMatch(/\.vs-button\[data-icon-only\] \{\s*padding: 0/);
    expect(css('inputs.css')).not.toMatch(/\.vs-button\[aria-label\]/);
  });
});

describe('a table whose header is hidden for a reader', () => {
  test('the hidden header has a positioned parent, or it escapes the page and scrolls the window', async () => {
    // `position: absolute` with no positioned ancestor up to the app's own region: the span sits at
    // its place in the flow, below the fold of the page's scroller, and the window grows a scrollbar.
    expect(css('surfaces.css')).toMatch(/\.vs-table-hidden-header \{\s*position: absolute/);
    expect(css('bars.css')).toMatch(/\.vs-bar-rows thead th \{[^}]*position: relative/);
    expect(css('surfaces.css')).toMatch(/\.vs-table th \{\s*position: sticky/);
  });
});

describe('Card with an action', () => {
  test('sets the link at the end of the heading line, and leaves a card without one as it was', async () => {
    const container = await mount(
      <>
        <Card
          section="Spend"
          action={<Button text="Details" variant="quiet" press={() => undefined} />}
        >
          Body
        </Card>
        <Card section="Plain">Body</Card>
      </>,
    );
    const [linked, plain] = container.querySelectorAll('section.vs-card');
    expect(linked?.querySelector('.vs-card-head h2')?.textContent).toBe('Spend');
    expect(linked?.querySelector('.vs-card-head button')?.textContent).toBe('Details');
    expect(plain?.querySelector('.vs-card-head')).toBeNull();
    expect(plain?.firstElementChild?.tagName).toBe('H2');
  });
});

describe('Split with the aside at the start', () => {
  test('keeps the main column first in the document, and reverses only the row', async () => {
    const container = await mount(
      <Split asideAtStart main={<span>Window</span>} aside={<span>List</span>} />,
    );
    const split = one(container, '.vs-split');
    expect(split.firstElementChild?.className).toBe('vs-split-main');
    expect(split.hasAttribute('data-aside-at-start')).toBe(true);
    expect(css('base.css')).toMatch(
      /\.vs-split\[data-aside-at-start\] \{\s*flex-direction: row-reverse/,
    );
  });
});

describe('tiles and charts on one set of tracks', () => {
  test('tiles on the chart track use the columns and the gap of ChartColumns', async () => {
    const container = await mount(
      <MetricTiles label="Objectives" count={2} track="chart">
        <MetricTile name="a" figure="1" />
        <MetricTile name="b" figure="2" />
      </MetricTiles>,
    );
    expect(one(container, 'section').getAttribute('data-track')).toBe('chart');
    const frame = css('frame.css');
    const chart =
      /\.vs-chart-columns \{[^}]*gap: var\(--space-6\)[^}]*minmax\(min\(100%, var\(--chart-min\)\)/s;
    const tiles =
      /\.vs-tiles\[data-track="chart"\] \{[^}]*repeat\(auto-fit, minmax\(min\(100%, var\(--chart-min\)\), 1fr\)\)[^}]*gap: var\(--space-6\)/s;
    expect(frame).toMatch(chart);
    expect(css('tiles.css')).toMatch(tiles);
  });

  test('columns of cards can be as tall as their own content', async () => {
    const container = await mount(
      <ChartColumns align="start">
        <Card>One</Card>
        <Card>Two</Card>
      </ChartColumns>,
    );
    expect(one(container, '.vs-chart-columns').getAttribute('data-align')).toBe('start');
    expect(css('frame.css')).toMatch(
      /\.vs-chart-columns\[data-align="start"\] \{\s*align-items: start/,
    );
  });
});

describe('StackedBars with few bars', () => {
  test('the plot and the labels under it are as wide as their bars may be, never the whole card', async () => {
    for (const count of [1, 3, 6]) {
      const bars = Array.from({length: count}, (_, at) => ({
        id: String(at),
        total: at + 1,
        title: `bar ${at}`,
        segments: [{key: `s${at}`, series: 0, value: at + 1}],
      }));
      const container = await mount(
        <StackedBars label="Spend" unit="dollars" bars={bars} marks={[{at: 0, text: '0'}]} />,
      );
      const figure = one(container, 'figure.vs-bars');
      expect(figure.style.getPropertyValue('--bars-count')).toBe(String(count));
    }
    const frame = css('frame.css');
    expect(frame).toMatch(/--bar-max: calc\(var\(--space-8\) \+ var\(--space-5\)\)/);
    expect(frame).toMatch(
      /\.vs-bars-plot,\s*\.vs-bars-marks,\s*\.vs-bars-axis \{[^}]*max-width: max\(var\(--chart-min\), calc\(var\(--bars-count\) \* \(var\(--bar-max\) \+ var\(--space-1\)\)\)\)/,
    );
    // The stacked area shares the axis row and counts no bars: a fallback count would squeeze its labels.
    expect(frame).not.toMatch(/var\(--bars-count,/);
  });
});

describe('Gantt with a legend', () => {
  test('keys what the bars, the line on them, the tag and the stack mean, and only when asked', async () => {
    const rows = [
      {
        id: 'a',
        label: 'lead',
        start: 0,
        end: 10,
        outcome: 'ok' as const,
        segments: [],
        press: () => undefined,
      },
    ];
    const words = {
      live: 'Growing',
      done: 'Over',
      failed: 'Broke',
      returned: 'Came back',
      tag: 'Where',
      folded: 'Stacked',
    };
    const keyed = await mount(<Gantt label="Agents" from={0} to={10} rows={rows} legend={words} />);
    const items = [...keyed.querySelectorAll('ul[aria-label="Key to the bars"] li')];
    expect(items.map(item => item.textContent)).toEqual([
      'Growing',
      'Over',
      'Broke',
      'Came back',
      ' Where',
      'Stacked',
    ]);
    const bare = await mount(<Gantt label="Agents" from={0} to={10} rows={rows} />);
    expect(bare.querySelector('ul[aria-label="Key to the bars"]')).toBeNull();
  });
});

describe('Page with a body that fills', () => {
  test('marks its scroll area, so one child takes the room and scrolls and the page does not', async () => {
    const container = await mount(
      <Page state="ready" title="Agent" fill>
        <Card>Above</Card>
        <div className="vs-conversation" data-size="fill" />
        <Card>Below</Card>
      </Page>,
    );
    const scroll = one(container, '.vs-page-scroll');
    expect(scroll.getAttribute('data-fill')).toBe('true');
    expect(scroll.firstElementChild?.className).toBe('vs-page-body');
    const plain = await mount(
      <Page state="ready" title="Cost">
        Body
      </Page>,
    );
    expect(one(plain, '.vs-page-scroll').hasAttribute('data-fill')).toBe(false);
  });

  test('has a body that gives its room to the conversation and a conversation as wide as its cards', () => {
    const base = css('base.css');
    expect(base).toMatch(/\.vs-page-scroll\[data-fill\]\s*\{[^}]*flex-direction:\s*column/);
    expect(base).toMatch(/\.vs-page-scroll\[data-fill\] > \.vs-page-body\s*\{[^}]*min-height:\s*0/);
    const conversation = css('conversation.css');
    // Its rows start on the page's edge: no gutter of its own, and no narrower measure than the cards.
    expect(conversation).toMatch(/\.vs-conversation-pane\s*\{[^}]*padding:\s*0 0 var\(--space-6\)/);
    expect(conversation).toMatch(
      /\[data-size="fill"\] > \.vs-conversation-pane\s*\{[^}]*max-width:\s*none/,
    );
    // A tail-following pane commonly starts part-way through a row. Its top guard keeps that
    // clipped row from showing as fragments underneath the fixed cards above the conversation.
    expect(conversation).toMatch(
      /\.vs-conversation\[data-size="fill"\]::before\s*\{[^}]*height:\s*var\(--space-4\)[^}]*background:\s*var\(--bg\)/,
    );
  });
});

describe('Gantt columns', () => {
  test('keeps the scrollbar gutter on the axis and on the rows, so the bars never shift under the ticks', () => {
    const frame = css('frame.css');
    for (const rows of frame.matchAll(/^\.vs-gantt-rows\s*\{([^}]*)\}/gm)) {
      expect(rows[1]).toContain('scrollbar-gutter: stable');
    }
    expect(frame).toMatch(
      /\.vs-gantt-row\[data-axis\]\s*\{[^}]*overflow:\s*hidden;[^}]*scrollbar-gutter:\s*stable/,
    );
  });

  test('drops a secondary tag in a narrow window but keeps a primary row identity', () => {
    const tree = css('gantt-tree.css');
    expect(tree).toMatch(
      /@media \(max-width: 760px\)[^@]*:not\(\[data-tag-priority="primary"\]\)[^@]*\.vs-gantt-tag\s*\{\s*display:\s*none/,
    );
  });

  test('gives the track the room a tag and a name do not need', () => {
    const tree = css('gantt-tree.css');
    expect(tree).toMatch(
      /clamp\(var\(--gantt-label\), 20%, var\(--gantt-label-max\)\)\s*var\(--gantt-tag\)/,
    );
  });

  test('gives a primary row-identity tag more room without hiding it at a narrow width', () => {
    const tree = css('gantt-tree.css');
    expect(tree).toMatch(
      /\[data-tag-priority="primary"\][^}]*grid-template-columns:[^}]*calc\(var\(--gantt-tag\) \+ var\(--space-8\) \* 2\)/,
    );
  });
});
