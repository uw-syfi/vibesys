/**
 * The start-run feature's welcome requests: a project's tasks, starting a run, and cancelling a
 * start. The run arguments cross as the text the user typed; the start-run feature owns their
 * syntax (`shellWords`), so the main process splits them where it acts on the start.
 */
import {type HostId, type HostKey, hostFromKey} from '@vibesys/hosts';
import {isTaskName} from '@vibesys/runs';
import type {StartOutcome, WelcomeResult} from './types.js';
import {type ChannelOf, operationToken, project} from './values.js';

export const START_RUN_CHANNELS = {
  tasks: 'vibesys:welcome:tasks',
  start: 'vibesys:welcome:start',
  cancelStart: 'vibesys:welcome:cancel-start',
} as const;

/** The welcome bridge's requests the start-run feature makes. */
export interface StartRunWelcomeBridge {
  tasks(host: HostKey, project: string): Promise<WelcomeResult<string[]>>;
  /** Start `task`; `start` names this start so that `cancelStart` can cancel it. */
  start(
    host: HostKey,
    project: string,
    task: string,
    args: string,
    start: number,
  ): Promise<WelcomeResult<StartOutcome>>;
  cancelStart(start: number): Promise<WelcomeResult<null>>;
}

export type StartRunRequest =
  | {readonly kind: 'tasks'; readonly host: HostId; readonly project: string}
  | {
      readonly kind: 'start';
      readonly host: HostId;
      readonly project: string;
      readonly task: string;
      /** The run arguments as typed, one line of shell words. */
      readonly args: string;
      readonly start: number;
    }
  | {readonly kind: 'cancel-start'; readonly start: number};

function runArguments(value: unknown): string {
  if (typeof value !== 'string') throw new Error('the run arguments must be text');
  return value;
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseStartRunRequest(
  channel: ChannelOf<typeof START_RUN_CHANNELS>,
  args: readonly unknown[],
): StartRunRequest {
  const [first, second, third, fourth, fifth] = args;
  switch (channel) {
    case START_RUN_CHANNELS.tasks:
      return {kind: 'tasks', host: hostFromKey(first), project: project(second)};
    case START_RUN_CHANNELS.start: {
      if (!isTaskName(third)) throw new Error("pick one of the project's tasks");
      return {
        kind: 'start',
        host: hostFromKey(first),
        project: project(second),
        task: third,
        args: runArguments(fourth),
        start: operationToken(fifth, 'start'),
      };
    }
    case START_RUN_CHANNELS.cancelStart:
      return {kind: 'cancel-start', start: operationToken(first, 'start')};
  }
}
