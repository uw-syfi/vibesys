/**
 * `screenCheck(page)`: the layout and wording checks over the screen a page shows now, against its
 * real layout. It returns every violation (`in-page.ts` says what each check reads); `report`
 * separates those a known failure explains (`known-failures.ts`) from the rest. The checks are deterministic: no timing, only the page's layout at the
 * moment of the call, so the caller fixes the viewport, theme and fonts and lets the page settle.
 */
import type {Page} from '@playwright/test';
import {FAILURE_REASONS} from '@vibesys/run-fixtures';
import {
  type CheckId,
  type CheckInput,
  chartSizeCheck,
  HELPERS,
  legendCheck,
  numberLabelCheck,
  overflowCheck,
  overlapCheck,
  rawTextCheck,
  strikeCheck,
  truncationCheck,
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
  'chart-size': chartSizeCheck,
};

/** Enum values that never read as words: the failure reasons spelled with a hyphen or underscore. */
const RAW_WORDS: readonly string[] = FAILURE_REASONS.filter(reason => /[-_]/.test(reason));

/** The script that defines the helpers and the checks in the page and runs `only` of them. */
function script(only: readonly PageCheckId[], input: CheckInput): string {
  const sources = [...HELPERS, ...Object.values(CHECKS)].map(each => each.toString());
  const calls = only.map(id => `...${CHECKS[id].name}(input)`);
  return `(() => {\n${sources.join('\n')}\nconst input = ${JSON.stringify(input)};\nreturn [${calls.join(', ')}];\n})()`;
}

/** Every violation of `only` (every check by default) on the screen `page` shows now. */
export async function screenCheck(
  page: Page,
  only: readonly PageCheckId[] = Object.keys(CHECKS) as PageCheckId[],
): Promise<Violation[]> {
  return page.evaluate<Violation[]>(script(only, {rawWords: RAW_WORDS}));
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
