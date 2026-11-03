/**
 * How a command the user issued is going. A command is sent once; an outcome that cannot be told
 * (a timeout, a lost link) is `Unconfirmed`, never re-sent by the app. It resolves by itself when
 * the daemon's receipt for that id arrives on the run stream, or by the user's explicit retry,
 * which reuses the command and its id.
 */
import {type Command, type Receipt, Refusal} from '@vibesys/orchestrator-proto';
import type {Fault} from './fault.js';

export type CommandProgress =
  | {readonly _tag: 'Sending'; readonly command: Command}
  | {readonly _tag: 'Accepted'; readonly receipt: Receipt}
  /** The daemon answered with a refusal: data, not an error. */
  | {readonly _tag: 'Refused'; readonly receipt: Receipt}
  /** The outcome is unknown. */
  | {readonly _tag: 'Unconfirmed'; readonly command: Command; readonly fault: Fault}
  /** Definitely not applied: the request was invalid or the daemon rejected it outright. */
  | {readonly _tag: 'Failed'; readonly command: Command; readonly fault: Fault};

/** A receipt as progress: accepted unless it carries a refusal. */
export function answered(receipt: Receipt): CommandProgress {
  return receipt.refusal === Refusal.UNSPECIFIED
    ? {_tag: 'Accepted', receipt}
    : {_tag: 'Refused', receipt};
}

/**
 * A command still waiting for its answer takes the receipt the run stream has folded for its id.
 * Settled progress, and progress with no receipt yet, stay as they are.
 */
export function settle(
  progress: CommandProgress,
  receipts: Readonly<Record<string, Receipt>>,
): CommandProgress {
  if (progress._tag !== 'Sending' && progress._tag !== 'Unconfirmed') return progress;
  const receipt = receipts[progress.command.id];
  return receipt === undefined ? progress : answered(receipt);
}
