/**
 * The run's summary: the newest report the lead posted. The lead writes it and the host numbers it,
 * so the newest is the one with the highest number (ties by the time it was recorded, then its id),
 * whatever order the messages arrived in.
 */
import {oneofIs, ReportKind} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../state.js';
import {ms} from '../time.js';

/** The parts of a report after its headline, in the order the lead gives them. */
export type SummaryField = 'changed' | 'inFlight' | 'decisions' | 'openForUser';

export interface SummaryView {
  readonly id: string;
  readonly headline: string;
  /** Only the parts the lead filled in. */
  readonly fields: readonly {readonly field: SummaryField; readonly text: string}[];
  /** The lead's last word, rather than a note along the way. */
  readonly final: boolean;
  readonly atMs: number;
  readonly sequence: number;
}

/** Every kind of message body, so a kind added to the contract is a compile error here. */
const BODY_KINDS = {
  text: true,
  handoff: true,
  report: true,
  jobFinished: true,
  subagentSettled: true,
  brief: true,
  leadBrief: true,
  handoffReminder: true,
  leadIdleReminder: true,
  runLimitReached: true,
  reportDue: true,
  catchUpRequest: true,
} as const;

const FIELDS: readonly SummaryField[] = ['changed', 'inFlight', 'decisions', 'openForUser'];

/** The newest report of the run, or undefined before the lead has posted one. */
export function latestReport(state: OrchestratorState, runId: string): SummaryView | undefined {
  const views: SummaryView[] = [];
  for (const message of Object.values(state.messages)) {
    if (message.runId !== runId || !oneofIs(message.body, 'report', BODY_KINDS)) continue;
    const report = message.body.value;
    views.push({
      id: message.id,
      headline: report.headline,
      fields: FIELDS.flatMap(field => (report[field] === '' ? [] : [{field, text: report[field]}])),
      final: report.kind === ReportKind.FINAL,
      atMs: ms(message.at) ?? 0,
      sequence: report.sequence,
    });
  }
  return views.reduce<SummaryView | undefined>(
    (newest, view) => (newest === undefined || isNewer(view, newest) ? view : newest),
    undefined,
  );
}

function isNewer(a: SummaryView, b: SummaryView): boolean {
  return (
    (a.sequence - b.sequence || a.atMs - b.atMs || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)) > 0
  );
}
