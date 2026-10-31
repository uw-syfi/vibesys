/**
 * `screenCheck(page)`: the layout and wording checks over the screen a page shows now, against its
 * real layout. It returns every violation (`in-page.ts` says what each check reads); `report`
 * separates those a known failure explains (`known-failures.ts`) from the rest. The checks are deterministic: no timing, only the page's layout at the
 * moment of the call, so the caller fixes the viewport, theme and fonts and lets the page settle.
 */
import type {Page} from '@playwright/test';
import {CODE_IDS, roleName} from '@vibesys/contracts';
import {FAILURE_REASONS} from '@vibesys/run-fixtures';
import {foreignFontUses} from '../../../../tools/e2e/bundled-fonts.mjs';
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
  legendCheck,
  numberLabelCheck,
  overflowCheck,
  overlapCheck,
  paneFillCheck,
  rawTextCheck,
  repeatCheck,
  scrollRoomCheck,
  strikeCheck,
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
  'chrome-clear': chromeClearCheck,
  'drag-region': dragRegionCheck,
};

/** Enum values that never read as words: the failure reasons spelled with a hyphen or underscore. */
const RAW_WORDS: readonly string[] = FAILURE_REASONS.filter(reason => /[-_]/.test(reason));

/** The ids the backend registries name, so the list of what must not show raw cannot drift. */
const REGISTRY_IDS: readonly string[] = [
  ...CODE_IDS.harnesses,
  ...CODE_IDS.models,
  ...CODE_IDS.roles,
];

/** The role names the catalog gives: none may be drawn in lowercase. */
const REGISTRY_NAMES: readonly string[] = CODE_IDS.roles.map(roleName);

/**
 * The names the fixture worlds' user typed: a project folder, an SSH alias, a task. They are the
 * user's own identifiers, shown as they were typed, not code names the app made up.
 */
const USER_NAMES: readonly string[] = [
  ...Array.from({length: 9}, (_, index) => `queue-rs-run${index + 1}`),
  'queue-rs',
  'queue-rs-2',
  'remote-host',
  'host-a',
  'host-b',
  'host-c',
  // The clusters and SSH aliases the machine Settings journeys type or list.
  'lab-h100',
  'lab-login',
  'amd-login',
  'cluster-m',
  'no-workspace',
  'no-scheduler',
  // An example's own name, which New run shows in the code face as the catalogue names it.
  'kv-store',
];

/** The script that defines the helpers and the checks in the page and runs `only` of them. */
function script(only: readonly PageCheckId[], input: CheckInput): string {
  const sources = [...HELPERS, ...Object.values(CHECKS)].map(each => each.toString());
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

/** Every violation of `only` (every check by default) on the screen `page` shows now. */
export async function screenCheck(
  page: Page,
  only: readonly (PageCheckId | 'bundled-font')[] = SCREEN_CHECKS,
): Promise<Violation[]> {
  const inPage = only.filter((id): id is PageCheckId => id !== 'bundled-font');
  const violations = await page.evaluate<Violation[]>(
    script(inPage, {
      rawWords: RAW_WORDS,
      ids: REGISTRY_IDS,
      names: REGISTRY_NAMES,
      userNames: USER_NAMES,
    }),
  );
  return only.includes('bundled-font')
    ? [...violations, ...(await bundledFontCheck(page))]
    : violations;
}

/** Whether pressing each of `points` (x, y) on the screen `page` shows moves the window, by its drag regions. */
export async function pressMovesWindow(
  page: Page,
  points: readonly (readonly [number, number])[],
): Promise<boolean[]> {
  const sources = HELPERS.map(each => each.toString());
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
