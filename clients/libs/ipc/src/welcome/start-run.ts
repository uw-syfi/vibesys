/**
 * The start-run feature's welcome requests: a project's tasks, the host's vetted harnesses,
 * starting a run, and cancelling a start. The run arguments cross as the text the user typed; the
 * start-run feature owns their syntax (`shellWords`), so the main process splits them where it acts
 * on the start. The harness crosses as a typed pair the host offered, never as text.
 */
import {type HostId, type HostKey, hostFromKey} from '@vibesys/hosts';
import {isTaskName} from '@vibesys/runs';
import type {StartOutcome, WelcomeResult} from './types.js';
import {type ChannelOf, operationToken, project} from './values.js';

export const START_RUN_CHANNELS = {
  tasks: 'vibesys:welcome:tasks',
  harnesses: 'vibesys:welcome:harnesses',
  start: 'vibesys:welcome:start',
  cancelStart: 'vibesys:welcome:cancel-start',
} as const;

/** One agent CLI provider and one model it runs, as the host's `harnesses.list` offered it. */
export interface HarnessChoice {
  readonly provider: string;
  readonly model: string;
}

/** A vetted pair; `default` marks the model a run on `provider` uses when nothing names one. */
export interface HarnessOffer extends HarnessChoice {
  readonly default: boolean;
}

/** The host's vetted harnesses, or `unsupported` when its helper predates the choice. */
export type HarnessListing =
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'listed'; readonly harnesses: readonly HarnessOffer[]};

/** The welcome bridge's requests the start-run feature makes. */
export interface StartRunWelcomeBridge {
  tasks(host: HostKey, project: string): Promise<WelcomeResult<string[]>>;
  harnesses(host: HostKey): Promise<WelcomeResult<HarnessListing>>;
  /**
   * Start `task` with `harness` (null: the project's configuration); `start` names this start so
   * that `cancelStart` can cancel it.
   */
  start(
    host: HostKey,
    project: string,
    task: string,
    args: string,
    harness: HarnessChoice | null,
    start: number,
  ): Promise<WelcomeResult<StartOutcome>>;
  cancelStart(start: number): Promise<WelcomeResult<null>>;
}

export type StartRunRequest =
  | {readonly kind: 'tasks'; readonly host: HostId; readonly project: string}
  | {readonly kind: 'harnesses'; readonly host: HostId}
  | {
      readonly kind: 'start';
      readonly host: HostId;
      readonly project: string;
      readonly task: string;
      /** The run arguments as typed, one line of shell words. */
      readonly args: string;
      readonly harness: HarnessChoice | null;
      readonly start: number;
    }
  | {readonly kind: 'cancel-start'; readonly start: number};

function runArguments(value: unknown): string {
  if (typeof value !== 'string') throw new Error('the run arguments must be text');
  return value;
}

/** The longest provider or model name a harness carries; the host checks the pair itself. */
const HARNESS_NAME_MAX = 256;

function harnessName(value: unknown): value is string {
  return typeof value === 'string' && value !== '' && value.length <= HARNESS_NAME_MAX;
}

/** A harness choice: null, or exactly a provider and a model name. */
function harnessChoice(value: unknown): HarnessChoice | null {
  if (value === null) return null;
  const problem = new Error('pick one of the vetted models, or the project default');
  if (typeof value !== 'object' || Array.isArray(value)) throw problem;
  const fields = value as Readonly<Record<string, unknown>>;
  const keys = Object.keys(fields).sort();
  if (keys.length !== 2 || keys[0] !== 'model' || keys[1] !== 'provider') throw problem;
  const {provider, model} = fields;
  if (!harnessName(provider) || !harnessName(model)) throw problem;
  return {provider, model};
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseStartRunRequest(
  channel: ChannelOf<typeof START_RUN_CHANNELS>,
  args: readonly unknown[],
): StartRunRequest {
  const [first, second, third, fourth, fifth, sixth] = args;
  switch (channel) {
    case START_RUN_CHANNELS.tasks:
      return {kind: 'tasks', host: hostFromKey(first), project: project(second)};
    case START_RUN_CHANNELS.harnesses:
      return {kind: 'harnesses', host: hostFromKey(first)};
    case START_RUN_CHANNELS.start: {
      if (!isTaskName(third)) throw new Error("pick one of the project's tasks");
      return {
        kind: 'start',
        host: hostFromKey(first),
        project: project(second),
        task: third,
        args: runArguments(fourth),
        harness: harnessChoice(fifth),
        start: operationToken(sixth, 'start'),
      };
    }
    case START_RUN_CHANNELS.cancelStart:
      return {kind: 'cancel-start', start: operationToken(first, 'start')};
  }
}
