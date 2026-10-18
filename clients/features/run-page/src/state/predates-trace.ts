/**
 * A run recorded before VibeSys recorded a run trace. Its journal holds no spans, which is where a
 * run's ideas and measurements are read from, so every face that would count them would say "no
 * ideas" or "nothing measured" about a run that tried plenty. The host says so
 * (`runs.summary`'s `trace`), and the page says what the run did not record instead of drawing
 * an empty run.
 */
import type {RunPageState} from './run-page.js';

/** How many rounds a run recorded before the run trace existed, or null when it has a trace. */
export function roundsBeforeTrace(state: RunPageState): number | null {
  const {summary} = state;
  return summary.kind === 'ready' && summary.trace === 'predates_trace'
    ? summary.roundsFinished
    : null;
}

/** What the panes say in place of ideas, measurements, the timeline and the agents. */
export function predatesTraceWords(rounds: number): {
  readonly title: string;
  readonly body: string;
} {
  const ran = rounds === 1 ? 'It ran 1 round' : `It ran ${rounds} rounds`;
  return {
    title: 'Recorded by an older VibeSys',
    body: `Ideas and measurements are not available for this run. ${ran}, but this version of VibeSys cannot read what the run found from that record.`,
  };
}
