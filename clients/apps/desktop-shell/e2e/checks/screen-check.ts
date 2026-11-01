/**
 * `screenCheck(page)`: the layout and wording checks over the screen a page shows now, against its
 * real layout. It returns every violation (`in-page.ts` says what each check reads); `report`
 * separates those a known failure explains (`known-failures.ts`) from the rest. A screen suite asks
 * for `SCREEN_CHECKS`, which adds the visual spec's laws; a journey's default stays with the defects. The checks are deterministic: no timing, only the page's layout at the
 * moment of the call, so the caller fixes the viewport, theme and fonts and lets the page settle.
 */
import type {Page} from '@playwright/test';
import {foreignFontUses} from '../../../../tools/e2e/bundled-fonts.mjs';
import {RAW_TEXT_HELPERS} from '../../src/testing/raw-text.js';
import {RAW_TEXT_INPUT} from '../../src/testing/raw-text-input.js';
import {
  bannerCheck,
  type CheckId,
  type CheckInput,
  chartSizeCheck,
  chromeClearCheck,
  columnTopsCheck,
  controlRowCheck,
  dragRegionCheck,
  focusRingCheck,
  HELPERS,
  headingDepthCheck,
  hollowBoxCheck,
  leftEdgeCheck,
  legendCheck,
  numberLabelCheck,
  overflowCheck,
  overlapCheck,
  paneFillCheck,
  rawTextCheck,
  repeatCheck,
  scrollRoomCheck,
  spacingOrderCheck,
  statusRowCheck,
  strikeCheck,
  titleStripCheck,
  truncationCheck,
  truncationRoomCheck,
  type Violation,
  wrapCheck,
} from './in-page.js';
import {isKnown, type KnownFailure} from './known-failures.js';

/**
 * The checks that run in the page; `stability` compares screens (`stability.ts`), and
 * `bundled-font` asks the browser which font drew each text (`foreignFontUses`).
 */
type PageCheckId = Exclude<CheckId, 'stability' | 'bundled-font'>;

const CHECKS: Readonly<Record<PageCheckId, (input: CheckInput) => Violation[]>> = {
  overlap: overlapCheck,
  wrap: wrapCheck,
  overflow: overflowCheck,
  truncation: truncationCheck,
  'raw-text': rawTextCheck,
  legend: legendCheck,
  'number-label': numberLabelCheck,
  strike: strikeCheck,
  repeat: repeatCheck,
  'chart-size': chartSizeCheck,
  'pane-fill': paneFillCheck,
  'scroll-room': scrollRoomCheck,
  'control-row': controlRowCheck,
  'column-tops': columnTopsCheck,
  'truncation-room': truncationRoomCheck,
  'focus-ring': focusRingCheck,
  banner: bannerCheck,
  'status-row': statusRowCheck,
  'chrome-clear': chromeClearCheck,
  'drag-region': dragRegionCheck,
  'left-edge': leftEdgeCheck,
  'heading-depth': headingDepthCheck,
  'spacing-order': spacingOrderCheck,
  'hollow-box': hollowBoxCheck,
  'title-strip': titleStripCheck,
};

/** The laws of the visual spec a page can decide: a suite that owns a screen's look runs these. */
const VISUAL_CHECKS: readonly PageCheckId[] = [
  'left-edge',
  'heading-depth',
  'spacing-order',
  'hollow-box',
  'title-strip',
];

/** The helpers every check may call: the layout ones and the wording ones, defined in one scope. */
const PAGE_HELPERS = [...RAW_TEXT_HELPERS, ...HELPERS];

/** The script that defines the helpers and the checks in the page and runs `only` of them. */
function script(only: readonly PageCheckId[], input: CheckInput): string {
  const sources = [...PAGE_HELPERS, ...Object.values(CHECKS)].map(each => each.toString());
  const calls = only.map(id => `...${CHECKS[id].name}(input)`);
  return `(() => {\n${sources.join('\n')}\nconst input = ${JSON.stringify(input)};\nreturn [${calls.join(', ')}];\n})()`;
}

/** The checks that run in the page, in the order they report. */
export const IN_PAGE_CHECKS = Object.keys(CHECKS) as PageCheckId[];

/** Every check of one screen: the in-page checks and the bundled-font check. */
export const SCREEN_CHECKS: readonly (PageCheckId | 'bundled-font')[] = [
  ...IN_PAGE_CHECKS,
  'bundled-font',
];

/** Text a font other than the bundled ones drew: a glyph its own font lacks falls back to the machine's. */
async function bundledFontCheck(page: Page): Promise<Violation[]> {
  return (await foreignFontUses(page)).map(use => ({
    check: 'bundled-font',
    message: `"${use.text}" is drawn by ${use.font}, not a bundled font: use var(--font-mono) or an icon`,
    where: use.where,
  }));
}

/** The checks a journey runs: the defects, not the visual spec's laws (a journey passing through a screen cannot pay that screen's debt). */
const JOURNEY_CHECKS: readonly (PageCheckId | 'bundled-font')[] = SCREEN_CHECKS.filter(
  id => id === 'bundled-font' || !VISUAL_CHECKS.includes(id),
);

/** Every violation of `only` (the journey checks by default) on the screen `page` shows now. */
export async function screenCheck(
  page: Page,
  only: readonly (PageCheckId | 'bundled-font')[] = JOURNEY_CHECKS,
): Promise<Violation[]> {
  const inPage = only.filter((id): id is PageCheckId => id !== 'bundled-font');
  const violations = await page.evaluate<Violation[]>(script(inPage, RAW_TEXT_INPUT));
  return only.includes('bundled-font')
    ? [...violations, ...(await bundledFontCheck(page))]
    : violations;
}

/** Whether pressing each of `points` (x, y) on the screen `page` shows moves the window, by its drag regions. */
export async function pressMovesWindow(
  page: Page,
  points: readonly (readonly [number, number])[],
): Promise<boolean[]> {
  const sources = PAGE_HELPERS.map(each => each.toString());
  return page.evaluate<boolean[]>(
    `(() => {\n${sources.join('\n')}\nconst regions = dragRegions();\nreturn ${JSON.stringify(points)}.map(([x, y]) => dragsAt(regions, x, y));\n})()`,
  );
}

/** One screen's violations that no known failure explains, and the known ones it matched. */
export interface ScreenReport {
  readonly unexplained: readonly Violation[];
  readonly known: readonly {readonly violation: Violation; readonly failure: KnownFailure}[];
}

export function report(
  screen: string,
  violations: readonly Violation[],
  failures: readonly KnownFailure[],
): ScreenReport {
  const known: {violation: Violation; failure: KnownFailure}[] = [];
  const unexplained: Violation[] = [];
  for (const violation of violations) {
    const failure = failures.find(each => isKnown(each, screen, violation));
    if (failure === undefined) unexplained.push(violation);
    else known.push({violation, failure});
  }
  return {unexplained, known};
}

/** What the screen draws that no person should read: raw ids, code names, lowercase names. */
export async function rawTextOf(page: Page): Promise<string[]> {
  const found = await screenCheck(page, ['raw-text']);
  return found.map(each => `${each.message} (${each.where})`);
}
