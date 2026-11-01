/**
 * The screen defects the checks found on main that no test change fixes: each is listed here with
 * the issue that tracks it, so the checks pass today and fail on anything new. A check is never
 * skipped; a known failure names the check, the text it matches and the screens it is seen on. The
 * list is a debt: the suite an entry belongs to fails when it matches nothing there
 * (`known-ledger.ts`), so fixing a defect means deleting its entry in the same change.
 *
 * Screens of the run matrix are named `<fixture>/<screen>/<width>` (plus `/paper` in the light
 * theme); those of the other suites start with what they show (`settings/<page>/<width>`).
 */
import type {CheckId, Violation} from './in-page.js';

/** The screen suites, one per spec: each sees only its own screens, so each owns its entries. */
export type Suite =
  | 'run-matrix'
  | 'run-history'
  | 'run-chat'
  | 'dashboard'
  | 'settings'
  | 'machines'
  | 'new-run'
  | 'usage';

export interface KnownFailure {
  /** A short name, for the failure message of a stale entry. */
  readonly id: string;
  readonly suite: Suite;
  readonly check: CheckId;
  /** Matches the violation's message. */
  readonly message: RegExp;
  /** Matches the screen's name; every screen when absent. */
  readonly screens?: RegExp;
  /** The issue that tracks the fix. */
  readonly issue: string;
}

/** The issue that brings every screen to the visual spec. */
const VISUAL_SPEC = 'https://github.com/uw-syfi/vibesys/issues/3500';

/** An entry of the visual spec's debt: a mechanism seen across the screens of one suite. */
function visual(
  id: string,
  suite: Suite,
  check: CheckId,
  message: RegExp,
  screens?: RegExp,
): KnownFailure {
  return {id, suite, check, message, issue: VISUAL_SPEC, ...(screens && {screens})};
}

const RUN_TITLE = /^the title "queue-rs · mpmc" is 18 px and a text under it is \d+ px/;
const SETTINGS_TITLE = /^the title "Settings" is 11\.5 px and a text under it is/;
const SETTINGS_BACK = /^the back control of "Settings" starts at x \d+ and the content/;
const SETTINGS_LABEL = /^the title of "Settings" starts at x \d+ and the content/;
/** A group label as close to the rows above it as to its own: the Settings nav's "This app". */
const NAV_LABEL = /^2 px between groups is not more than the 2 px inside one: "Agent logins"/;
const NAV_FIELD = /^2 px between groups is not more than the 6 px inside one: "Machine/;
/** A stack nested in a stack of the same gap: nothing tells the groups from their rows. */
const EVEN_GAPS = /^(\d+) px between groups is not more than the \1 px inside one/;
const RUN_HEADER_GAPS = /^4 px between groups is not more than the 4 px inside one: "queue-rs/;

export const KNOWN_FAILURES: readonly KnownFailure[] = [
  visual('run-title-matrix', 'run-matrix', 'heading-depth', RUN_TITLE),
  visual('run-title-history', 'run-history', 'heading-depth', RUN_TITLE),
  visual(
    'run-usage-face-gaps',
    'run-matrix',
    'spacing-order',
    /^8 px between groups is not more than the 12 px inside one: "IdeasTimeline/,
  ),
  visual('run-header-gaps-rail', 'dashboard', 'spacing-order', RUN_HEADER_GAPS),
  visual('chat-even-gaps', 'run-chat', 'spacing-order', EVEN_GAPS),
  visual(
    'chat-turn-gaps',
    'run-chat',
    'spacing-order',
    /^0 px between groups is not more than the 2 px inside one/,
  ),
  visual('settings-title-rail', 'dashboard', 'heading-depth', SETTINGS_TITLE),
  visual('settings-back-rail', 'dashboard', 'left-edge', SETTINGS_BACK),
  visual('settings-label-rail', 'dashboard', 'left-edge', SETTINGS_LABEL),
  visual('settings-nav-label-rail', 'dashboard', 'spacing-order', NAV_LABEL),
  visual('settings-nav-field-rail', 'dashboard', 'spacing-order', NAV_FIELD),
  visual('settings-title', 'settings', 'heading-depth', SETTINGS_TITLE),
  visual('settings-back', 'settings', 'left-edge', SETTINGS_BACK),
  visual('settings-label', 'settings', 'left-edge', SETTINGS_LABEL),
  visual('settings-nav-label', 'settings', 'spacing-order', NAV_LABEL),
  visual('settings-nav-field', 'settings', 'spacing-order', NAV_FIELD),
  visual('settings-even-gaps', 'settings', 'spacing-order', EVEN_GAPS),
  visual(
    'settings-field-gaps',
    'settings',
    'spacing-order',
    /^4 px between groups is not more than the 6 px inside one: "SSH host alias/,
  ),
  visual(
    'settings-wide-cards',
    'settings',
    'hollow-box',
    /under the 40% a box fits: "(Modal|SkyPilot|campus|cluster-m)/,
  ),
  visual('settings-wide-empty', 'settings', 'hollow-box', /a box fits: "No Slurm cluster yet"/),
  visual('new-run-even-gaps', 'new-run', 'spacing-order', EVEN_GAPS),
  visual(
    'new-run-roles-gaps',
    'new-run',
    'spacing-order',
    /^8 px between groups is not more than the 12 px inside one: "Use a different agent/,
  ),
  visual(
    'new-run-wide-cards',
    'new-run',
    'hollow-box',
    /under the 40% a box fits: "(Benchmark|Optimizes)/,
  ),
];

export function isKnown(failure: KnownFailure, screen: string, violation: Violation): boolean {
  return (
    failure.check === violation.check &&
    failure.message.test(violation.message) &&
    (failure.screens?.test(screen) ?? true)
  );
}
