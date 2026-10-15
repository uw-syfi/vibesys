/**
 * The process boundary for Electron's single-instance launch handoff.
 *
 * Electron may reorder or append to the `argv` reported by its `second-instance` event. The
 * secondary process therefore sends the exact, already-stripped desktop arguments and the cwd
 * VibeSys used for its own initial parsing in versioned additional data. This module validates that
 * untrusted value before the existing launch parser is allowed to interpret it.
 */
import {type LaunchPlan, parseLaunch} from './launch-args.js';

const HANDOFF_VERSION = 1;
const HANDOFF_KEYS = new Set(['version', 'argv', 'cwd']);

type LaunchHandoff = Readonly<{
  version: typeof HANDOFF_VERSION;
  argv: readonly string[];
  cwd: string;
}>;

export type PreparedLaunch = Readonly<{
  plan: LaunchPlan;
  handoff: LaunchHandoff;
}>;

/** Everything Electron reports for a second instance, including its non-authoritative argv. */
export type SecondInstanceLaunch = Readonly<{
  electronArgv: readonly string[];
  electronCwd: string;
  additionalData: unknown;
}>;

export class LaunchHandoffError extends Error {
  override name = 'LaunchHandoffError';
}

/**
 * Parse this process's launch and retain exactly the same inputs for a possible handoff.
 *
 * `argv` is already stripped of Electron's executable/application prefix. The returned object is
 * JSON-compatible and owns its argument array, so later changes to the caller's array cannot alter
 * what the primary receives.
 */
export function prepareLaunchHandoff(argv: readonly string[], cwd: string): PreparedLaunch {
  const handoff: LaunchHandoff = {version: HANDOFF_VERSION, argv: [...argv], cwd};
  return {plan: planFromHandoff(handoff), handoff};
}

/**
 * Recover a forwarded plan only from the validated additional data.
 *
 * The event `argv` and `cwd` are deliberately present in the input shape but never consulted. That
 * makes the Electron boundary's fail-closed policy explicit and testable: malformed additional
 * data cannot fall back to Chromium-mutated argv or any other event metadata.
 */
export function planFromSecondInstance(launch: SecondInstanceLaunch): LaunchPlan {
  return planFromHandoff(launch.additionalData);
}

function planFromHandoff(input: unknown): LaunchPlan {
  const handoff = parseHandoff(input);
  return parseLaunch(handoff.argv, handoff.cwd);
}

function parseHandoff(input: unknown): LaunchHandoff {
  if (typeof input !== 'object' || input === null || Array.isArray(input)) {
    throw new LaunchHandoffError('launch handoff must be an object');
  }
  const record = input as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (!HANDOFF_KEYS.has(key)) throw new LaunchHandoffError(`launch handoff.${key} is unknown`);
  }

  const version = required(record, 'version');
  if (version !== HANDOFF_VERSION) {
    throw new LaunchHandoffError(`launch handoff.version must be ${HANDOFF_VERSION}`);
  }

  const argvValue = required(record, 'argv');
  if (!Array.isArray(argvValue)) {
    throw new LaunchHandoffError('launch handoff.argv must be an array');
  }
  const argv: string[] = [];
  for (let index = 0; index < argvValue.length; index += 1) {
    const argument = argvValue[index];
    if (typeof argument !== 'string') {
      throw new LaunchHandoffError(`launch handoff.argv[${index}] must be a string`);
    }
    argv.push(argument);
  }

  const cwd = required(record, 'cwd');
  if (typeof cwd !== 'string' || cwd.length === 0) {
    throw new LaunchHandoffError('launch handoff.cwd must be a non-empty string');
  }
  return {version: HANDOFF_VERSION, argv, cwd};
}

function required(record: Record<string, unknown>, key: string): unknown {
  if (!Object.hasOwn(record, key))
    throw new LaunchHandoffError(`launch handoff.${key} is required`);
  return record[key];
}
