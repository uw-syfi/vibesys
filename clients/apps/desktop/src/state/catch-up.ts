/**
 * How a press of Catch me up is going, as a screen reads it: the command's progress
 * (`state/commands.ts`) told in what this feature distinguishes. The host decides everything about
 * the press (the window, the facts, whether the lead is asked); the app only says whether the host
 * answered, refused, or could not be reached, and with which check-in.
 *
 * A refusal is the host's decision and is data, never an error. The host also refuses a press that
 * names no viewer with `InvalidArgument` naming the metadata field (the call is refused before it
 * is decided, so no receipt carries it), which is read here as the same refusal.
 */

import {Refusal} from '@vibesys/orchestrator-proto';
import type {CommandProgress} from './commands.js';
import type {Fault} from './fault.js';

/** Why the host would not make a check-in. */
export const CATCH_UP_REFUSALS = [
  'run-not-found',
  'no-viewer',
  'not-permitted',
  'unexpected',
] as const;
export type CatchUpRefusal = (typeof CATCH_UP_REFUSALS)[number];

export type CatchUpPress =
  /** The press is on its way to the host. */
  | {readonly _tag: 'Pressing'}
  /** The host made the check-in (or answered with the one it was still working out). */
  | {readonly _tag: 'Made'; readonly checkInId: string}
  | {readonly _tag: 'Refused'; readonly reason: CatchUpRefusal}
  /** The outcome is not known: pressing again sends the same command under the same id. */
  | {readonly _tag: 'Unconfirmed'; readonly fault: Fault}
  /** The press did not reach a decision and nothing was made. */
  | {readonly _tag: 'Failed'; readonly fault: Fault};

/** The metadata fields of a viewer, which the host names when a call lacks them. */
const VIEWER_FIELDS = ['vibesys-viewer-id', 'vibesys-viewer-label'] as const;

export function catchUpRefusalOf(refusal: Refusal): CatchUpRefusal {
  switch (refusal) {
    case Refusal.RUN_NOT_FOUND:
      return 'run-not-found';
    case Refusal.NO_VIEWER:
      return 'no-viewer';
    case Refusal.NOT_PERMITTED:
      return 'not-permitted';
    default:
      // Any other refusal is one a catch-up cannot get: say so rather than guess at words for it.
      return 'unexpected';
  }
}

const namesAViewerField = (detail: string): boolean =>
  VIEWER_FIELDS.some(field => detail.includes(field));

/** A press as the screen reads it; `undefined` before any press of the run. */
export function pressOf(progress: CommandProgress | undefined): CatchUpPress | undefined {
  if (progress === undefined) return undefined;
  switch (progress._tag) {
    case 'Sending':
      return {_tag: 'Pressing'};
    case 'Accepted':
      return progress.receipt.checkInId === ''
        ? {
            _tag: 'Failed',
            fault: {
              kind: 'InvalidMessage',
              detail: 'the host answered with no check-in',
              retryable: false,
            },
          }
        : {_tag: 'Made', checkInId: progress.receipt.checkInId};
    case 'Refused':
      return {_tag: 'Refused', reason: catchUpRefusalOf(progress.receipt.refusal)};
    case 'Unconfirmed':
      return {_tag: 'Unconfirmed', fault: progress.fault};
    case 'Failed':
      return progress.fault.kind === 'Rejected' && namesAViewerField(progress.fault.detail)
        ? {_tag: 'Refused', reason: 'no-viewer'}
        : {_tag: 'Failed', fault: progress.fault};
  }
}
