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
  MetricTile,
  MetricTiles,
  Page,
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

  test('keeps the trail band without a trail, so a title is at one place on every page', async () => {
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
    for (const container of [titled, own]) {
      const header = one(container, '.vs-page-header');
      expect(header.firstElementChild?.className).toBe('vs-page-trail');
      expect(header.querySelector('.vs-crumb')).toBeNull();
      expect(header.querySelector('h1')).not.toBeNull();
    }
    // The band is as tall wherever it is, so the heading below it never moves between pages.
    expect(css('base.css')).toMatch(/\.vs-page-trail \{[^}]*min-height: var\(--row-dense\)/);
    // And the title row is a control tall, so a title with a button beside it is where one without is.
    expect(css('base.css')).toMatch(/\.vs-page-title-row \{[^}]*min-height: var\(--control\)/);
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
    const chart = /\.vs-chart-columns \{[^}]*gap: var\(--space-6\)[^}]*minmax\(var\(--chart-min\)/s;
    const tiles =
      /\.vs-tiles\[data-track="chart"\] \{[^}]*minmax\(min\(100%, var\(--chart-min\)\), 1fr\)[^}]*gap: var\(--space-6\)/s;
    expect(frame).toMatch(chart);
    expect(frame).toMatch(tiles);
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
      /\.vs-bars-plot,\s*\.vs-bars-marks \{\s*max-width: calc\(var\(--bars-count, 1\) \* \(var\(--bar-max\) \+ var\(--space-1\)\)\)/,
    );
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
    const keyed = await mount(<Gantt label="Agents" from={0} to={10} rows={rows} legend />);
    const items = [...keyed.querySelectorAll('ul[aria-label="Key to the bars"] li')];
    expect(items.map(item => item.textContent)).toEqual([
      'Running',
      'Done',
      'Failed',
      'Result returned',
      'host-aMachine',
      'Folded',
    ]);
    const bare = await mount(<Gantt label="Agents" from={0} to={10} rows={rows} />);
    expect(bare.querySelector('ul[aria-label="Key to the bars"]')).toBeNull();
  });
});
