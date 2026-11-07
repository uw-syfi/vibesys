/**
 * The run's summary: the report the host serves as the one that sums the run up (`RunSummary.report`).
 * The host picks it (the lead's newest report about the whole run, never one that answered Catch me
 * up) and this only reads it into a view: nothing here looks through messages for the newest.
 */
import {ReportKind} from '@vibesys/orchestrator-proto';
import {messageIs} from '../messages.js';
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

const FIELDS: readonly SummaryField[] = ['changed', 'inFlight', 'decisions', 'openForUser'];

/** The report the host sums the run up by, or undefined before the host has sent one. */
export function summaryReport(state: OrchestratorState, runId: string): SummaryView | undefined {
  const message = state.summaries[runId]?.report;
  if (!messageIs(message, 'report')) return undefined;
  const report = message.body.value;
  return {
    id: message.id,
    headline: report.headline,
    fields: FIELDS.flatMap(field => (report[field] === '' ? [] : [{field, text: report[field]}])),
    final: report.kind === ReportKind.FINAL,
    atMs: ms(message.at) ?? 0,
    sequence: report.sequence,
  };
}
