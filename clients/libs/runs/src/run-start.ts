/**
 * What a run was started with beyond its project and task: the harness, the typed options and the
 * extra arguments of `run.start`. The app keeps it with the run it started (the recent list), so
 * "Run again" can start from the same options. The shapes are the helper contract's own; the
 * parser validates a stored or received value and names the first offending key.
 */
import type {HelperRequest} from '@vibesys/contracts/host-helper';

/** The typed options a run starts with (`run.start`'s `options`). */
export type RunOptions = NonNullable<Extract<HelperRequest, {type: 'run.start'}>['options']>;
/** One agent role's model and reasoning effort within the run's harness. */
export type RoleOption = NonNullable<RunOptions['roles']>[number];
/** A provider and one of its vetted models. */
export type HarnessPick = NonNullable<Extract<HelperRequest, {type: 'run.start'}>['harness']>;

/** The start inputs other than project and task: what Run again prefills. */
export interface RunStart {
  readonly harness: HarnessPick | null;
  readonly options: RunOptions;
  /** Advanced arguments, split into words. */
  readonly args: readonly string[];
}

const EFFORTS = ['default', 'low', 'medium', 'high'] as const;
const RESULTS = ['keep', 'publish_github'] as const;

function record(value: unknown, what: string, keys: readonly string[]): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${what} must be an object`);
  }
  const fields = value as Record<string, unknown>;
  const unknown = Object.keys(fields).find(key => !keys.includes(key));
  if (unknown !== undefined) throw new Error(`${what}.${unknown} is not a known field`);
  return fields;
}

function text(value: unknown, what: string): string {
  if (typeof value !== 'string' || value.trim() === '' || value.includes('\n')) {
    throw new Error(`${what} must be one line of text`);
  }
  return value;
}

function count(value: unknown, what: string): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${what} must be a positive whole number`);
  }
  return value;
}

function oneOf<T extends string>(value: unknown, values: readonly T[], what: string): T {
  const found = values.find(each => each === value);
  if (found === undefined) throw new Error(`${what} must be one of ${values.join(', ')}`);
  return found;
}

function role(value: unknown, at: number): RoleOption {
  const fields = record(value, `options.roles[${at}]`, ['role', 'model', 'reasoning_effort']);
  const model = fields['model'];
  return {
    role: text(fields['role'], `options.roles[${at}].role`),
    model: model === null || model === undefined ? null : text(model, `options.roles[${at}].model`),
    reasoning_effort: oneOf(
      fields['reasoning_effort'] ?? 'default',
      EFFORTS,
      `options.roles[${at}].reasoning_effort`,
    ),
  };
}

function options(value: unknown): RunOptions {
  const fields = record(value, 'options', ['rounds', 'time_limit_s', 'results', 'roles']);
  const roles = fields['roles'] ?? [];
  if (!Array.isArray(roles)) throw new Error('options.roles must be a list');
  return {
    rounds: count(fields['rounds'], 'options.rounds'),
    time_limit_s: count(fields['time_limit_s'], 'options.time_limit_s'),
    results:
      fields['results'] === null || fields['results'] === undefined
        ? null
        : oneOf(fields['results'], RESULTS, 'options.results'),
    roles: roles.map(role),
  };
}

function harness(value: unknown): HarnessPick | null {
  if (value === null) return null;
  const fields = record(value, 'harness', ['provider', 'model']);
  return {
    provider: text(fields['provider'], 'harness.provider'),
    model: text(fields['model'], 'harness.model'),
  };
}

function words(value: unknown): readonly string[] {
  if (
    !Array.isArray(value) ||
    value.some(word => typeof word !== 'string' || word.includes('\n'))
  ) {
    throw new Error('args must be a list of one-line words');
  }
  return value as string[];
}

/**
 * The start inputs in `fields` (`harness`, `options`, `args`) as a `RunStart`, or an `Error` naming
 * the first offending key (`options.roles[0].model`, `args`).
 */
export function parseRunStart(fields: {
  readonly harness: unknown;
  readonly options: unknown;
  readonly args: unknown;
}): RunStart {
  return {
    harness: harness(fields.harness),
    options: options(fields.options),
    args: words(fields.args),
  };
}

/** `value` as a `RunStart` (an object with exactly `harness`, `options` and `args`). */
export function parseStoredRunStart(value: unknown): RunStart {
  const fields = record(value, 'start', ['harness', 'options', 'args']);
  return parseRunStart({
    harness: fields['harness'] ?? null,
    options: fields['options'] ?? {},
    args: fields['args'] ?? [],
  });
}
