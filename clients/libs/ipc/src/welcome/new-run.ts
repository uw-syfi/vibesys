/**
 * The New run feature's welcome requests: what a machine offers, the "Ready to start" checks for a
 * set of inputs, the bundled examples, the two ways of making a project its own repository, and
 * starting a run with typed options. Each is one host helper method; the main process answers
 * them over the host's connection.
 */
import type {
  Backend,
  Check,
  CreatedProject,
  ExampleDestination,
  ExampleList,
  HelperFeature,
  MachineDescription,
  TaskGoal,
} from '@vibesys/contracts/host-helper';
import {type HostId, type HostKey, hostFromKey} from '@vibesys/hosts';
import {
  type HarnessPick,
  type MeasureTarget,
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
  taskGoal: 'vibesys:welcome:task-goal',
  exampleDestination: 'vibesys:welcome:example-destination',
  helperFeatures: 'vibesys:welcome:helper-features',
} as const;

export type {HarnessPick, MeasureTarget, RoleOption, RunOptions};

/** What the inputs of a New run are: the checks are for exactly these, and a start uses them. */
export interface RunInputs extends RunStart {
  readonly project: string;
  readonly task: string;
}

/** What a helper offers when it is too old for a method. */
export type Offered<T> =
  | {readonly kind: 'offered'; readonly value: T}
  | {readonly kind: 'unsupported'};

/** What a preflight answers: the checks for the inputs, and the roles of the run's orchestration. */
export interface PreflightResult {
  readonly checks: readonly Check[];
  /**
   * The role ids the run's orchestration declares, in declaration order; null from a helper that
   * does not take `run_roles`. Empty when the settings did not resolve an orchestration.
   */
  readonly roles: readonly string[] | null;
  /**
   * The compute backend the run resolved to, which its task declares; null from a helper that does
   * not take `run_backend`, or when the settings resolved none.
   */
  readonly backend: Backend | null;
}

/** What an example's destination holds (`examples.destination`). */
export interface DestinationFound {
  readonly state: ExampleDestination;
  /** The project's directory when `state` is `example_project`; null otherwise. */
  readonly project: string | null;
}

/** What making a project from an example came to: the project, or a destination that holds something. */
export type ExampleCreation =
  | {readonly kind: 'created'; readonly created: CreatedProject}
  | {readonly kind: 'taken'};

/** A run that started: its id and the server that drives it. */
export interface StartedRun {
  readonly runId: string;
  readonly instanceId: string;
}

/** The welcome bridge's requests the New run feature makes. */
export interface NewRunWelcomeBridge {
  describeMachine(host: HostKey): Promise<WelcomeResult<Offered<MachineDescription>>>;
  preflight(host: HostKey, inputs: RunInputs): Promise<WelcomeResult<Offered<PreflightResult>>>;
  examples(host: HostKey): Promise<WelcomeResult<Offered<ExampleList>>>;
  createFromExample(
    host: HostKey,
    example: string,
    destination: string,
  ): Promise<WelcomeResult<ExampleCreation>>;
  copyOut(
    host: HostKey,
    source: string,
    destination: string,
  ): Promise<WelcomeResult<CreatedProject>>;
  startRun(host: HostKey, inputs: RunInputs): Promise<WelcomeResult<StartedRun>>;
  /** The goal text and objectives of `task` in the user's own `project` on `host`. */
  taskGoal(host: HostKey, project: string, task: string): Promise<WelcomeResult<Offered<TaskGoal>>>;
  /** What creating `example` at `destination` on `host` would find there. */
  exampleDestination(
    host: HostKey,
    example: string,
    destination: string,
  ): Promise<WelcomeResult<Offered<DestinationFound>>>;
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
  | {
      readonly kind: 'task-goal';
      readonly host: HostId;
      readonly project: string;
      readonly task: string;
    }
  | {
      readonly kind: 'example-destination';
      readonly host: HostId;
      readonly example: string;
      readonly destination: string;
    }
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
  const fields = record(value, 'inputs', [
    'project',
    'task',
    'harness',
    'options',
    'agents',
    'measure_on',
    'args',
  ]);
  return {
    project: project(fields['project']),
    task: text(fields['task'], 'inputs.task'),
    ...parseRunStart({
      harness: fields['harness'],
      options: fields['options'],
      agents: fields['agents'],
      measure_on: fields['measure_on'],
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
    case NEW_RUN_CHANNELS.taskGoal:
      return {
        kind: 'task-goal',
        host,
        project: project(second),
        task: text(third, 'the task'),
      };
    case NEW_RUN_CHANNELS.exampleDestination:
      return {
        kind: 'example-destination',
        host,
        example: text(second, 'the example'),
        destination: project(third),
      };
    case NEW_RUN_CHANNELS.helperFeatures:
      return {kind: 'helper-features', host};
  }
}
