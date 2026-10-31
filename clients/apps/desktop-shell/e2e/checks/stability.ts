/**
 * The stability check: what a screen keeps while the user moves through it. The performance chart,
 * its row and the time bar are the same elements, in the same place and size, at every level and
 * while the run is scrubbed; an element replaced by a copy (a remount) or moved by a pixel reads as
 * a jump to the user. `anchorsOf` records the anchors on the screen now; `stabilityViolations`
 * compares the screen later against what was recorded.
 *
 * An anchor's place is its place in the screen's body, whatever the body is scrolled to (the Ideas
 * table is under the charts, so a reader of a laptop window has scrolled to it). The scroll itself
 * is held too: a press that scrolled the page is a violation, except where the page got shorter
 * than the scroll it was at and the browser took the scroll back to the end that is left, and
 * (`mayScroll`) the press that opens the Ideas table's idea, which draws the list again beside the
 * idea: its rows are measured after their first draw, and the layout between the two shortens the
 * page for a moment and takes the scroll with it.
 */
import type {Page} from '@playwright/test';
import type {Violation} from './in-page.js';

/** An element that must stay put: how the page names it, and the selector that finds it. */
export interface Anchor {
  readonly name: string;
  readonly selector: string;
  /** The element may be absent on some screens (a level that has no such control). */
  readonly optional?: boolean;
  /** The element may be a new one on each screen (each draws its own copy), as long as the copy is in the same box. */
  readonly copies?: boolean;
}

/** The elements the Run screen keeps through every level and the scrubber. */
export const RUN_ANCHORS: readonly Anchor[] = [
  {
    name: 'performance chart',
    selector: '[role="group"][aria-label*="of each reading over the run"]',
  },
  {name: 'time bar', selector: '[role="slider"][aria-label="Run position"]'},
  {
    name: 'face switcher',
    selector: '[role="radiogroup"][aria-label="Show"]:has(input[value="timeline"])',
    optional: true,
    copies: true,
  },
];

interface Placed {
  readonly name: string;
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
  /** The screen body's scroll when this was read. */
  readonly scrolled: number;
}

/** Records each anchor's element (kept in the page) and where it is; an absent one is `null`. */
export async function anchorsOf(
  page: Page,
  anchors: readonly Anchor[] = RUN_ANCHORS,
): Promise<readonly (Placed | null)[]> {
  return page.evaluate(wanted => {
    const kept = new Map<string, Element>();
    const scrolled = document.querySelector('.vs-screen-body')?.scrollTop ?? 0;
    const placed = wanted.map(anchor => {
      const element = document.querySelector(anchor.selector);
      if (element === null) return null;
      kept.set(anchor.name, element);
      const {x, y, width, height} = element.getBoundingClientRect();
      return {name: anchor.name, x, y: y + scrolled, width, height, scrolled};
    });
    (window as unknown as {screenAnchors: Map<string, Element>}).screenAnchors = kept;
    return placed;
  }, anchors);
}

/**
 * What changed for the anchors recorded by `anchorsOf` as `before`: an anchor that is gone, that is
 * a different element now, or that moved or changed size by more than half a pixel.
 */
export async function stabilityViolations(
  page: Page,
  step: string,
  before: readonly (Placed | null)[],
  anchors: readonly Anchor[] = RUN_ANCHORS,
  mayScroll = false,
): Promise<Violation[]> {
  const {now, body} = await page.evaluate(wanted => {
    const kept = (window as unknown as {screenAnchors: Map<string, Element>}).screenAnchors;
    const element = document.querySelector('.vs-screen-body');
    const scrolled = element?.scrollTop ?? 0;
    return {
      body: {
        scrolled,
        end: Math.max(0, (element?.scrollHeight ?? 0) - (element?.clientHeight ?? 0)),
      },
      now: wanted.map(anchor => {
        const found = document.querySelector(anchor.selector);
        if (found === null) return null;
        const {x, y, width, height} = found.getBoundingClientRect();
        return {
          name: anchor.name,
          x,
          y: y + scrolled,
          width,
          height,
          scrolled,
          same: kept.get(anchor.name) === found,
        };
      }),
    };
  }, anchors);
  const found: Violation[] = mayScroll ? [] : [...scrollViolation(step, before, body)];
  for (const [index, was] of before.entries()) {
    if (was === null) continue;
    const is = now[index];
    const anchor = anchors[index];
    const absent = is === null || is === undefined;
    if (absent && anchor?.optional === true) continue;
    const problem = absent ? 'is gone' : movement(was, is, anchor?.copies === true);
    if (problem !== null) {
      found.push({
        check: 'stability',
        message: `the ${was.name} ${problem} at "${step}"`,
        where: anchors[index]?.selector ?? was.name,
      });
    }
  }
  return found;
}

function movement(was: Placed, is: Placed & {same: boolean}, copies: boolean): string | null {
  if (!is.same && !copies) return 'was replaced by a new element (a remount)';
  const keys = ['x', 'y', 'width', 'height'] as const;
  const moved = keys.filter(key => Math.abs(was[key] - is[key]) > 0.5);
  return moved.length === 0
    ? null
    : `changed its ${moved.map(key => `${key} from ${was[key]} to ${is[key]}`).join(', ')}`;
}

/** The press scrolled the page, which only a page that got shorter than its scroll may do (the browser clamps it). */
function scrollViolation(
  step: string,
  before: readonly (Placed | null)[],
  body: {readonly scrolled: number; readonly end: number},
): Violation[] {
  const was = before.find(each => each !== null)?.scrolled;
  if (was === undefined || Math.abs(was - body.scrolled) <= 0.5) return [];
  const clamped = body.scrolled < was && body.scrolled >= body.end - 0.5;
  return clamped
    ? []
    : [
        {
          check: 'stability',
          message: `the page scrolled from ${was} to ${body.scrolled} at "${step}"`,
          where: '.vs-screen-body',
        },
      ];
}
