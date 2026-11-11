import {Brief, type BriefField, Button} from '@vibesys/ui';
import type {JSX} from 'react';
import type {SummaryField, SummaryView} from '../../../state/index.js';
import {withShortCommits} from '../../commit-text.js';
import {formatAge} from './format.js';

/** The parts' labels: what was running is past tense once the run is over, or it would contradict its state. */
const fieldLabel = (field: SummaryField, over: boolean): string =>
  ({changed: 'Changed', inFlight: over ? 'Was running' : 'Running', decisions: 'Decided'})[field];

/** Whose it is and how old; for a run that is over, a report that was not the final one is the last it wrote, not a state of now. */
function labelOf(summary: SummaryView, nowMs: number, over: boolean): string {
  if (over && !summary.final) return "Lead's last summary · before the run ended";
  return `Lead's ${summary.final ? 'final ' : ''}summary · ${formatAge(nowMs - summary.atMs)}`;
}

/**
 * The lead's latest report as one block under the facts: whose it is and how old, its headline (cut
 * at two lines) and the parts it filled in, each under its label (cut at three lines; a part the
 * lead left empty is left out, label and all). What the lead writes has no bound on its length, so
 * it is set in body sizes only, a long word breaks inside itself, and whatever is cut is whole on
 * hover and in Catch me up, which one link under the block opens, when anything is cut. A run whose
 * lead has not reported says only that, in one line.
 */
export function LeadSummary(props: {
  readonly summary: SummaryView | undefined;
  readonly nowMs: number;
  /** The run has ended or failed: its last report is not about now. */
  readonly over: boolean;
  /** Open the run's Catch me up page, where the whole report is. */
  readonly openCatchUp: () => void;
}): JSX.Element {
  const {summary} = props;
  if (summary === undefined) return <Brief empty="The lead has not reported yet" />;
  const fields: BriefField[] = summary.fields
    .filter(({text}) => text.trim() !== '')
    .map(({field, text}) => ({
      label: fieldLabel(field, props.over),
      text: withShortCommits(text),
      title: text,
    }));
  return (
    <Brief
      name="Lead summary"
      label={labelOf(summary, props.nowMs, props.over)}
      {...(summary.headline.trim() === ''
        ? {}
        : {lead: withShortCommits(summary.headline), leadTitle: summary.headline})}
      fields={fields}
      more={
        <Button text="Read the whole report" variant="quiet" size="sm" press={props.openCatchUp} />
      }
    />
  );
}
