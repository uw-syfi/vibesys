/**
 * The requests that list what a run can start with: a project's tasks and the host's vetted
 * harnesses. A harness crosses as a typed pair the host offered, never as text.
 */
import {type HostId, type HostKey, hostFromKey} from '@vibesys/hosts';
import type {WelcomeResult} from './types.js';
import {type ChannelOf, project} from './values.js';

export const START_RUN_CHANNELS = {
  tasks: 'vibesys:welcome:tasks',
  harnesses: 'vibesys:welcome:harnesses',
} as const;

/** One agent CLI provider and one model it runs, as the host's `harnesses.list` offered it. */
export interface HarnessChoice {
  readonly provider: string;
  readonly model: string;
}

/** How a pair reads, as agentshim names it; a client renders these and never derives a name from an id. */
interface HarnessDisplay {
  /** The provider's CLI as people call it ("Claude Code"). */
  readonly providerName: string;
  /** The model alone ("Opus 5.5"); absent when agentshim does not name it: show the raw id. */
  readonly modelName?: string;
  /** The model line ("Claude", "GPT", "Gemini"); absent exactly when `modelName` is. */
  readonly family?: string;
  /** The model's rank in its family, 1 the smallest and cheapest; absent off the ladder. */
  readonly tier?: number;
  /** The tier as people call it ("Opus"); absent exactly when `tier` is. */
  readonly tierName?: string;
  /** Who made the model; absent from a host that does not report makers. */
  readonly maker?: 'anthropic' | 'openai' | 'google';
  /** The generation of the maker's models it belongs to; absent when the host names none. */
  readonly generation?: {readonly id: string; readonly name: string; readonly order: number};
}

/**
 * A vetted pair; `default` marks the model a run on `provider` uses when nothing names one.
 * `display` is absent when the host's helper predates it: the pair is then listed flat.
 */
export interface HarnessOffer extends HarnessChoice {
  readonly default: boolean;
  readonly display?: HarnessDisplay;
}

/** The host's vetted harnesses, or `unsupported` when its helper predates the choice. */
export type HarnessListing =
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'listed'; readonly harnesses: readonly HarnessOffer[]};

/** The welcome bridge's requests the start-run feature makes. */
export interface StartRunWelcomeBridge {
  tasks(host: HostKey, project: string): Promise<WelcomeResult<string[]>>;
  harnesses(host: HostKey): Promise<WelcomeResult<HarnessListing>>;
}

export type StartRunRequest =
  | {readonly kind: 'tasks'; readonly host: HostId; readonly project: string}
  | {readonly kind: 'harnesses'; readonly host: HostId};

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseStartRunRequest(
  channel: ChannelOf<typeof START_RUN_CHANNELS>,
  args: readonly unknown[],
): StartRunRequest {
  const [first, second] = args;
  switch (channel) {
    case START_RUN_CHANNELS.tasks:
      return {kind: 'tasks', host: hostFromKey(first), project: project(second)};
    case START_RUN_CHANNELS.harnesses:
      return {kind: 'harnesses', host: hostFromKey(first)};
  }
}
