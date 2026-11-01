import {availableParallelism} from 'node:os';

/** The variable that names how many Playwright workers one run uses. */
export const WORKERS_VARIABLE = 'VIBESYS_E2E_WORKERS';

/**
 * How many workers a run starts when nothing names a count: the cores of a hosted CI runner (four)
 * and of the shared development machines the suite is run on, whichever is fewer. Each worker is a
 * Chromium and a Node process that both stay busy, so more workers than cores only add contention.
 */
export const DEFAULT_WORKERS = 4;

/**
 * The variables that make a spec write screenshots and a contact sheet of them. A sheet is built
 * from the screens the worker that runs its spec file walked, so a run that asks for one needs
 * every screen of a file in one worker: it runs on one. A spec that reads a new variable of this
 * kind is listed here, and `e2e_workers.test.mjs` fails until it is.
 */
export const CAPTURE_VARIABLES: readonly string[] = [
  'LEVEL_SHOTS',
  'MACHINES_SCREENSHOTS',
  'NO_BANNERS_DIR',
  'QUOTA_SHOTS',
  'RUN_PAGE_SHOTS',
  'RUN_SHOTS',
  'SCREENS_DIR',
  'TOASTS_SCREENSHOTS',
  'USAGE_SCREENS_DIR',
];

/**
 * The worker count of the run: `VIBESYS_E2E_WORKERS` when set (a positive whole number), else
 * `DEFAULT_WORKERS` capped by the machine's cores, and one when the run captures screens. A value
 * that is not a positive whole number, or that asks for several workers while a capture variable
 * is set, fails the run naming the variable, so neither is read as a count nobody chose.
 */
export function e2eWorkers(
  env: Readonly<Record<string, string | undefined>> = process.env,
  cores: number = availableParallelism(),
): number {
  const captured = CAPTURE_VARIABLES.filter(name => (env[name] ?? '') !== '');
  const value = env[WORKERS_VARIABLE];
  if (value === undefined || value === '') {
    return captured.length > 0 ? 1 : Math.max(1, Math.min(DEFAULT_WORKERS, cores));
  }
  const count = /^[1-9][0-9]*$/.test(value) ? Number(value) : Number.NaN;
  if (!Number.isSafeInteger(count)) {
    throw new Error(`${WORKERS_VARIABLE} must be a positive whole number, got "${value}"`);
  }
  if (count > 1 && captured.length > 0) {
    throw new Error(
      `${captured.join(', ')} write a contact sheet from one worker's screens: ${WORKERS_VARIABLE} must be 1, got ${count}`,
    );
  }
  return count;
}
