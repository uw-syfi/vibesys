import {readdirSync, readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

/** The slice of a Playwright suite one CI job runs, in the shape Playwright's `shard` option takes. */
export interface E2eShard {
  readonly current: number;
  readonly total: number;
}

/**
 * The shard named by `variable` (`"2/5"` is the second of five), or `undefined` when it is unset or
 * empty, which runs the whole suite as a local run does. CI sets it so each job takes one slice of
 * every app's specs; together the slices run every spec exactly once. A malformed value fails the
 * run by name instead of silently running everything, which would hide a mis-sized matrix.
 */
export function e2eShard(
  variable: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): E2eShard | undefined {
  const value = env[variable];
  if (value === undefined || value === '') return undefined;
  const match = /^([1-9][0-9]*)\/([1-9][0-9]*)$/.exec(value);
  const current = Number(match?.[1]);
  const total = Number(match?.[2]);
  if (
    match === null ||
    !Number.isSafeInteger(current) ||
    !Number.isSafeInteger(total) ||
    current > total
  ) {
    throw new Error(`${variable} must look like "2/5" with 1 <= current <= total, got "${value}"`);
  }
  return {current, total};
}

/** One global, stable file plan across the apps, weighted by recorded Linux test seconds. */
export function assignE2eSpecs(
  specs: readonly string[],
  seconds: Readonly<Record<string, number>>,
  total: number,
): ReadonlyMap<string, number> {
  if (!Number.isSafeInteger(total) || total < 1) throw new Error('shard total must be positive');
  if (new Set(specs).size !== specs.length) throw new Error('duplicate e2e spec path');
  for (const [path, duration] of Object.entries(seconds)) {
    if (!Number.isFinite(duration) || duration <= 0)
      throw new Error(`${path}: invalid test seconds`);
  }
  const measured = Object.values(seconds).sort((a, b) => a - b);
  // New files use the measured median until the next full-suite timing refresh.
  const fallback = measured[Math.floor(measured.length / 2)] ?? 1;
  const weight = (path: string): number => seconds[path] ?? fallback;
  const ordered = [...specs].sort().sort((a, b) => weight(b) - weight(a));
  const loads = Array<number>(total).fill(0);
  const assigned = new Map<string, number>();
  for (const path of ordered) {
    const chosen = lightest(loads);
    loads[chosen] = (loads[chosen] ?? 0) + weight(path);
    assigned.set(path, chosen + 1);
  }
  return assigned;
}

function lightest(loads: readonly number[]): number {
  let chosen = 0;
  for (let index = 1; index < loads.length; index += 1) {
    if ((loads[index] ?? 0) < (loads[chosen] ?? 0)) chosen = index;
  }
  return chosen;
}

const APPS = new URL('../../apps/', import.meta.url);
// Linux test seconds per file, recorded from complete app runs and the gallery CI job.
const TIMINGS = new URL('./durations.json', import.meta.url);
const TEST_FILE = /[.](?:spec|test)\.[cm]?[jt]sx?$/;

/** Playwright's test files, discovered from every app's e2e directory, never an allowlist. */
export function e2eSpecs(): string[] {
  return readdirSync(APPS, {withFileTypes: true})
    .filter(entry => entry.isDirectory())
    .flatMap(app => specFiles(new URL(`${app.name}/`, APPS), `${app.name}/`, false))
    .sort();
}

function specFiles(directory: URL, prefix: string, insideE2e: boolean): string[] {
  return readdirSync(directory, {withFileTypes: true}).flatMap(entry => {
    if (entry.isDirectory()) {
      if (entry.name.startsWith('.') || entry.name === 'node_modules') return [];
      // Only enter the app's test root, not its source, dist or screenshot artifacts.
      if (!insideE2e && entry.name !== 'e2e') return [];
      return specFiles(new URL(`${entry.name}/`, directory), `${prefix}${entry.name}/`, true);
    }
    return insideE2e && TEST_FILE.test(entry.name) ? [`${prefix}${entry.name}`] : [];
  });
}

/**
 * The specs `VIBESYS_E2E_SPECS` selects (a JSON array of spec paths), or `undefined` when it is
 * unset or empty. A path that is not a spec fails the run by name instead of silently selecting
 * nothing, which would hide a stale selection.
 */
export function e2eSelection(
  specs: readonly string[],
  env: Readonly<Record<string, string | undefined>> = process.env,
): string[] | undefined {
  const value = env['VIBESYS_E2E_SPECS'];
  if (value === undefined || value === '') return undefined;
  const parsed: unknown = JSON.parse(value);
  if (!Array.isArray(parsed) || parsed.some(path => typeof path !== 'string')) {
    throw new Error('VIBESYS_E2E_SPECS must be a JSON array of spec paths');
  }
  const selected = parsed as string[];
  const unknown = selected.filter(path => !specs.includes(path));
  if (unknown.length > 0) throw new Error(`VIBESYS_E2E_SPECS names no spec: ${unknown.join(', ')}`);
  return [...new Set(selected)].sort();
}

/**
 * This app's exact files in the shared plan; no shard variable keeps all its files.
 * The configs do not also use Playwright's count-based shard, which would split the plan twice.
 * A shard may own no file of one app; the app commands use --pass-with-no-tests for that case.
 */
export function e2eTestMatch(
  app: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): string[] {
  const specs = e2eSpecs();
  if (!specs.some(path => path.startsWith(`${app}/`))) throw new Error(`${app}: no e2e specs`);
  // The plan covers the selected specs when a pull request runs only those a change can affect,
  // and every spec otherwise (the merge queue and a local run).
  const planned = e2eSelection(specs, env) ?? specs;
  const owned = planned.filter(path => path.startsWith(`${app}/`));
  const shard = e2eShard('VIBESYS_E2E_SHARD', env);
  if (shard === undefined) return owned.map(path => `**/${path}`);
  const raw: unknown = JSON.parse(readFileSync(TIMINGS, 'utf8'));
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new Error(`${fileURLToPath(TIMINGS)}: expected spec paths and test seconds`);
  }
  const seconds: Record<string, number> = {};
  for (const [path, value] of Object.entries(raw)) {
    if (typeof value !== 'number' || !specs.includes(path)) {
      throw new Error(`${fileURLToPath(TIMINGS)}: invalid spec or seconds for ${path}`);
    }
    seconds[path] = value;
  }
  const assigned = assignE2eSpecs(planned, seconds, shard.total);
  return owned.filter(path => assigned.get(path) === shard.current).map(path => `**/${path}`);
}
