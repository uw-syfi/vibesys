/**
 * The New run feature's welcome requests: what a machine offers, the "Ready to start" checks for a
 * set of inputs, the bundled examples, the two ways of making a project its own repository, and
 * starting a run with typed options. Each is one host helper method; the main process answers
 * them over the host's connection.
 */
import type {
  Check,
  CreatedProject,
  ExampleList,
  HelperFeature,
  MachineDescription,
} from '@vibesys/contracts/host-helper';
import {type HostId, type HostKey, hostFromKey} from '@vibesys/hosts';
import {
  type HarnessPick,
  parseRunStart,
  type RoleOption,
  type RunOptions,
  type RunStart,
} from '@vibesys/runs';
import type {WelcomeResult} from './types.js';
import {type ChannelOf, project} from './values.js';

export const NEW_RUN_CHANNELS = {
  describeMachine: 'vibesys:welcome:describe-machine',
  preflight: 'vibesys:welcome:preflight',
  examples: 'vibesys:welcome:examples',
  createFromExample: 'vibesys:welcome:create-from-example',
  copyOut: 'vibesys:welcome:copy-out',
  startRun: 'vibesys:welcome:start-run',
  helperFeatures: 'vibesys:welcome:helper-features',
} as const;

export type {HarnessPick, RoleOption, RunOptions};

/** What the inputs of a New run are: the checks are for exactly these, and a start uses them. */
export interface RunInputs extends RunStart {
  readonly project: string;
  readonly task: string;
}

/** What a helper offers when it is too old for a method. */
export type Offered<T> =
  | {readonly kind: 'offered'; readonly value: T}
  | {readonly kind: 'unsupported'};

/** A run that started: its id and the server that drives it. */
export interface StartedRun {
  readonly runId: string;
  readonly instanceId: string;
}

/** The welcome bridge's requests the New run feature makes. */
export interface NewRunWelcomeBridge {
  describeMachine(host: HostKey): Promise<WelcomeResult<Offered<MachineDescription>>>;
  preflight(host: HostKey, inputs: RunInputs): Promise<WelcomeResult<Offered<readonly Check[]>>>;
  examples(host: HostKey): Promise<WelcomeResult<Offered<ExampleList>>>;
  createFromExample(
    host: HostKey,
    example: string,
    destination: string,
  ): Promise<WelcomeResult<CreatedProject>>;
  copyOut(
    host: HostKey,
    source: string,
    destination: string,
  ): Promise<WelcomeResult<CreatedProject>>;
  startRun(host: HostKey, inputs: RunInputs): Promise<WelcomeResult<StartedRun>>;
  /** The features `host`'s helper agreed to: what New run may ask of it. */
  helperFeatures(host: HostKey): Promise<WelcomeResult<readonly HelperFeature[]>>;
}

export type NewRunRequest =
  | {readonly kind: 'describe-machine'; readonly host: HostId}
  | {readonly kind: 'preflight'; readonly host: HostId; readonly inputs: RunInputs}
  | {readonly kind: 'examples'; readonly host: HostId}
  | {
      readonly kind: 'create-from-example';
      readonly host: HostId;
      readonly example: string;
      readonly destination: string;
    }
  | {
      readonly kind: 'copy-out';
      readonly host: HostId;
      readonly source: string;
      readonly destination: string;
    }
  | {readonly kind: 'start-run'; readonly host: HostId; readonly inputs: RunInputs}
  | {readonly kind: 'helper-features'; readonly host: HostId};

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

/** `value` as `RunInputs`, or an `Error` naming the first offending key. */
function parseRunInputs(value: unknown): RunInputs {
  const fields = record(value, 'inputs', ['project', 'task', 'harness', 'options', 'args']);
  return {
    project: project(fields['project']),
    task: text(fields['task'], 'inputs.task'),
    ...parseRunStart({
      harness: fields['harness'],
      options: fields['options'],
      args: fields['args'],
    }),
  };
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseNewRunRequest(
  channel: ChannelOf<typeof NEW_RUN_CHANNELS>,
  args: readonly unknown[],
): NewRunRequest {
  const [first, second, third] = args;
  const host = hostFromKey(first);
  switch (channel) {
    case NEW_RUN_CHANNELS.describeMachine:
      return {kind: 'describe-machine', host};
    case NEW_RUN_CHANNELS.preflight:
      return {kind: 'preflight', host, inputs: parseRunInputs(second)};
    case NEW_RUN_CHANNELS.examples:
      return {kind: 'examples', host};
    case NEW_RUN_CHANNELS.createFromExample:
      return {
        kind: 'create-from-example',
        host,
        example: text(second, 'the example'),
        destination: project(third),
      };
    case NEW_RUN_CHANNELS.copyOut:
      return {kind: 'copy-out', host, source: project(second), destination: project(third)};
    case NEW_RUN_CHANNELS.startRun:
      return {kind: 'start-run', host, inputs: parseRunInputs(second)};
    case NEW_RUN_CHANNELS.helperFeatures:
      return {kind: 'helper-features', host};
  }
}
