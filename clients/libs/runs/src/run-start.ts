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
type MeasureOnField = Extract<HelperRequest, {type: 'run.start'}>['measure_on'];

/** Where results are measured (`run.start`'s `measure_on`): this machine, a cluster of it, or Modal. */
export type MeasureTarget = NonNullable<MeasureOnField>;

/** The start inputs other than project and task: what Run again prefills. */
export interface RunStart {
  readonly harness: HarnessPick | null;
  readonly options: RunOptions;
  /** The agent of each role that does not use the run's, by role id (`run.start`'s `agents`). */
  readonly agents: Readonly<Record<string, HarnessPick>>;
  /** Where results are measured; the same machine unless chosen. */
  readonly measure_on: MeasureTarget;
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

function agents(value: unknown): Readonly<Record<string, HarnessPick>> {
  if (value === null || value === undefined) return {};
  const fields = record(value, 'agents', Object.keys(value as object));
  return Object.fromEntries(
    Object.entries(fields).map(([name, each]) => {
      const pick = harness(each);
      if (pick === null) throw new Error(`agents.${name} must be an object`);
      return [name, pick];
    }),
  );
}

function measureOn(value: unknown): MeasureTarget {
  if (value === null || value === undefined) return {kind: 'same_machine'};
  const kind = record(value, 'measure_on', ['kind', 'cluster'])['kind'];
  switch (kind) {
    case 'same_machine':
    case 'modal':
      record(value, 'measure_on', ['kind']);
      return {kind};
    case 'slurm':
      return {
        kind,
        cluster: text((value as {cluster?: unknown}).cluster, 'measure_on.cluster'),
      };
    default:
      throw new Error('measure_on.kind must be one of same_machine, slurm, modal');
  }
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
 * the first offending key (`options.roles[0].model`, `agents.judge.model`, `args`).
 */
export function parseRunStart(fields: {
  readonly harness: unknown;
  readonly options: unknown;
  readonly agents?: unknown;
  readonly measure_on?: unknown;
  readonly args: unknown;
}): RunStart {
  return {
    harness: harness(fields.harness),
    options: options(fields.options),
    agents: agents(fields.agents),
    measure_on: measureOn(fields.measure_on),
    args: words(fields.args),
  };
}

/** `value` as a `RunStart` (an object with `harness`, `options`, `args`, and `agents` when it has any). */
export function parseStoredRunStart(value: unknown): RunStart {
  const fields = record(value, 'start', ['harness', 'options', 'agents', 'measure_on', 'args']);
  return parseRunStart({
    harness: fields['harness'] ?? null,
    options: fields['options'] ?? {},
    agents: fields['agents'] ?? null,
    measure_on: fields['measure_on'] ?? null,
    args: fields['args'] ?? [],
  });
}
