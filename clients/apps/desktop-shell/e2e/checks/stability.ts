/**
 * The stability check: what a screen keeps while the user moves through it. The performance chart,
 * its row and the time bar are the same elements, in the same place and size, at every level and
 * while the run is scrubbed; an element replaced by a copy (a remount) or moved by a pixel reads as
 * a jump to the user. `anchorsOf` records the anchors on the screen now; `stabilityViolations`
 * compares the screen later against what was recorded.
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
}

/** Records each anchor's element (kept in the page) and where it is; an absent one is `null`. */
export async function anchorsOf(
  page: Page,
  anchors: readonly Anchor[] = RUN_ANCHORS,
): Promise<readonly (Placed | null)[]> {
  return page.evaluate(wanted => {
    const kept = new Map<string, Element>();
    const placed = wanted.map(anchor => {
      const element = document.querySelector(anchor.selector);
      if (element === null) return null;
      kept.set(anchor.name, element);
      const {x, y, width, height} = element.getBoundingClientRect();
      return {name: anchor.name, x, y, width, height};
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
): Promise<Violation[]> {
  const now = await page.evaluate(wanted => {
    const kept = (window as unknown as {screenAnchors: Map<string, Element>}).screenAnchors;
    return wanted.map(anchor => {
      const element = document.querySelector(anchor.selector);
      if (element === null) return null;
      const {x, y, width, height} = element.getBoundingClientRect();
      return {name: anchor.name, x, y, width, height, same: kept.get(anchor.name) === element};
    });
  }, anchors);
  const found: Violation[] = [];
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
