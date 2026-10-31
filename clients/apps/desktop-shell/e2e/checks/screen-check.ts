/**
 * `screenCheck(page)`: the layout and wording checks over the screen a page shows now, against its
 * real layout. It returns every violation (`in-page.ts` says what each check reads); `report`
 * separates those a known failure explains (`known-failures.ts`) from the rest. A screen suite asks
 * for `EVERY_CHECK`, which adds the visual spec's laws; a journey's default stays with the defects. The checks are deterministic: no timing, only the page's layout at the
 * moment of the call, so the caller fixes the viewport, theme and fonts and lets the page settle.
 */
import type {Page} from '@playwright/test';
import {CODE_IDS, roleName} from '@vibesys/contracts';
import {FAILURE_REASONS} from '@vibesys/run-fixtures';
import {
  type CheckId,
  type CheckInput,
  chartSizeCheck,
  columnTopsCheck,
  controlRowCheck,
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
  strikeCheck,
  truncationCheck,
  truncationRoomCheck,
  type Violation,
  wrapCheck,
} from './in-page.js';
import {isKnown, type KnownFailure} from './known-failures.js';

/** The checks that read one screen; `stability` compares screens (`stability.ts`). */
type PageCheckId = Exclude<CheckId, 'stability'>;

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
  'left-edge': leftEdgeCheck,
  'heading-depth': headingDepthCheck,
  'spacing-order': spacingOrderCheck,
  'hollow-box': hollowBoxCheck,
};

/** The laws of the visual spec a page can decide: a suite that owns a screen's look runs these. */
const VISUAL_CHECKS: readonly PageCheckId[] = [
  'left-edge',
  'heading-depth',
  'spacing-order',
  'hollow-box',
];

/** Every check. A screen suite reports these against the known failures. */
export const EVERY_CHECK = Object.keys(CHECKS) as PageCheckId[];
const JOURNEY_CHECKS = EVERY_CHECK.filter(id => !VISUAL_CHECKS.includes(id));

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

/** Every violation of `only` (the journey checks by default) on the screen `page` shows now. */
export async function screenCheck(
  page: Page,
  only: readonly PageCheckId[] = JOURNEY_CHECKS,
): Promise<Violation[]> {
  return page.evaluate<Violation[]>(
    script(only, {
      rawWords: RAW_WORDS,
      ids: REGISTRY_IDS,
      names: REGISTRY_NAMES,
      userNames: USER_NAMES,
    }),
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
