/** The words the run controls say about requests, apart from the components that show them. */
import {IntentAction} from '@vibesys/runs/intents';
import type {IntentRow} from './machine.js';

/** What each kind of request is called in the history. */
export const ACTION_LABELS: Record<IntentAction, string> = {
  [IntentAction.UNSPECIFIED]: 'Request',
  [IntentAction.PAUSE]: 'Pause',
  [IntentAction.PARK]: 'Pause',
  [IntentAction.PAUSE_NOW]: 'Pause now',
  [IntentAction.RESUME]: 'Resume',
  [IntentAction.STOP]: 'Stop',
  [IntentAction.STEER]: 'Guidance',
  [IntentAction.WITHDRAW]: 'Withdraw guidance',
  [IntentAction.CANCEL_ATTEMPT]: 'Cancel attempt',
  [IntentAction.CANCEL_ITEM]: 'Cancel item',
  [IntentAction.SET_MAX_PARALLEL]: 'Change parallelism',
};

/** An error handed to the run chat: where it happened, what it said, and its diagnostic. */
export interface ErrorReport {
  readonly where: string;
  readonly message: string;
  readonly detail: string | null;
}

/** The reason a refused or unconfirmed request shows under its row; null for the rest. */
export function shownReason(row: IntentRow): string | null {
  return (row.status === 'refused' || row.status === 'uncertain') && row.reason ? row.reason : null;
}

/** A refused or unconfirmed request, as the run chat is asked about it; null for the rest. */
export function refusalReport(row: IntentRow): ErrorReport | null {
  const reason = shownReason(row);
  if (reason === null) return null;
  return {where: `${ACTION_LABELS[row.action]} request`, message: reason, detail: row.detail};
}

/** The run controls' updates failing, as the run chat is asked about it. */
export function updatesReport(error: string): ErrorReport {
  return {where: 'Run controls', message: error, detail: null};
}
