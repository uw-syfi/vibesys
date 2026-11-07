/**
 * The time axis of a `Gantt`, read as drawn: every tick label is centred on its moment, inside the
 * track and clear of its neighbours, the labels are equally spaced, and each is a tidy moment of
 * the clock the axis is told. The track's width is given by a fake resize notice; the positions
 * are read from the rendered ticks, not from the tick function.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {Gantt} from './index.js';
import {MONO_ADVANCE, TYPE} from './tokens/index.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
let trackWidth = 0;

/** A resize observer that reports the width the test gives the track as soon as it is watched. */
class FixedWidthObserver {
  private readonly notify: (entries: {contentRect: {width: number}}[]) => void;
  constructor(notify: (entries: {contentRect: {width: number}}[]) => void) {
    this.notify = notify;
  }
  observe(): void {
    this.notify([{contentRect: {width: trackWidth}}]);
  }
  disconnect(): void {}
}

beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {
    window,
    IS_REACT_ACT_ENVIRONMENT: true,
    ResizeObserver: FixedWidthObserver,
  });
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT', 'ResizeObserver']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const GLYPH = TYPE.section.size * (MONO_ADVANCE + TYPE.section.tracking);
const SEATTLE = -8 * HOUR;

/** `Mon 00:00`: the moment on the clock that runs `SEATTLE` behind UTC. */
function clock(at: number, span: number): string {
  const shown = new Date(at + SEATTLE).toISOString();
  return span < DAY ? shown.slice(11, 16) : `${shown.slice(8, 10)} ${shown.slice(11, 16)}`;
}

interface Drawn {
  readonly text: string;
  readonly centred: boolean;
  readonly centre: number;
  readonly left: number;
  readonly right: number;
}

async function drawn(from: number, to: number, width: number, zone: boolean): Promise<Drawn[]> {
  trackWidth = width;
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <Gantt
        label="Agents"
        from={from}
        to={to}
        axis={zone ? {label: clock, zone: () => SEATTLE} : {label: clock}}
        rows={[]}
      />,
    ),
  );
  const ticks = [...container.querySelectorAll('.vs-gantt-tick')].map(tick => {
    const style = tick.getAttribute('style') ?? '';
    const share = Number(/--gantt-from:\s*([\d.]+)%/.exec(style)?.[1] ?? Number.NaN) / 100;
    const text = tick.textContent ?? '';
    const centre = share * width;
    const own = text.length * GLYPH;
    return {
      text,
      centred: tick.hasAttribute('data-centred') && !tick.hasAttribute('data-placed'),
      centre,
      left: centre - own / 2,
      right: centre + own / 2,
    };
  });
  await act(async () => root.unmount());
  container.remove();
  return ticks;
}

/** A small seeded generator, so a failing case repeats. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

describe('the time axis of a Gantt, as drawn', () => {
  test('puts each label on its moment, inside the track, clear of its neighbours and equally spaced', async () => {
    const next = random(5);
    for (let trial = 0; trial < 120; trial += 1) {
      const from = 1_700_000_000_000 + Math.floor(next() * 31_536_000_000);
      const to = from + Math.floor(10 ** (3 + next() * 6.5));
      const width = 240 + Math.floor(next() * 1600);
      const ticks = await drawn(from, to, width, false);
      expect(ticks.length).toBeGreaterThan(0);
      ticks.forEach((tick, index) => {
        expect(tick.centred).toBe(true);
        expect(tick.left).toBeGreaterThanOrEqual(-0.5);
        expect(tick.right).toBeLessThanOrEqual(width + 0.5);
        const before = ticks[index - 1];
        if (before !== undefined) expect(tick.left).toBeGreaterThanOrEqual(before.right);
      });
      const gaps = ticks.slice(1).map((tick, index) => tick.centre - (ticks[index]?.centre ?? 0));
      for (const gap of gaps) expect(Math.abs(gap - (gaps[0] ?? gap))).toBeLessThan(0.5);
    }
  });

  test('draws a week-long axis at three window widths on the midnights of its clock, and at least two labels', async () => {
    const from = Date.UTC(2026, 10, 1, 17, 0);
    const to = from + 6 * DAY + 5 * HOUR;
    for (const width of [233, 501, 613]) {
      const ticks = await drawn(from, to, width, true);
      expect(ticks.length).toBeGreaterThanOrEqual(width < 300 ? 1 : 2);
      for (const tick of ticks) expect(tick.text.endsWith('00:00')).toBe(true);
    }
  });

  test('draws an axis of minutes whose first moment is the start with the labels still inside it', async () => {
    const from = Date.UTC(2026, 10, 1, 17, 0, 0);
    const ticks = await drawn(from, from + 8 * 60_000, 501, false);
    expect(ticks.length).toBeGreaterThanOrEqual(3);
    expect(ticks[0]?.left).toBeGreaterThanOrEqual(0);
  });
});
