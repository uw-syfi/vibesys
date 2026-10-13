/**
 * The run page: header, connectivity banners, summary, and activity. Presentational: it renders
 * the run and the session it is given and reports clicks through its callbacks.
 */

import type {RunCommand, RunCommandOutcome, WebSessionState} from '@vibesys/run-session';
import {
  type CoreState,
  type IterationNavigationState,
  iterationRoots,
  iterationRootsForCore,
} from '@vibesys/run-state';
import {type RoundKey, roundsWithPlan, sameRoundKey} from '@vibesys/run-state/rounds';
import {type JSX, type ReactNode, useState} from 'react';
import {
  connectionBanners,
  emptyTranscriptCopy,
  pageHeader,
  REPLAY_SESSION,
} from '../../state/banners.js';
import {Banners} from './banners.js';
import {RoundRail} from './round-rail.js';
import {RunControls} from './run-controls.js';
import {RunSummary} from './run-summary.js';
import {Transcript} from './transcript.js';

export interface RunPageProps {
  readonly headerAction?: ReactNode;
  readonly run: CoreState;
  readonly iterations: IterationNavigationState;
  /** The live session's state; null on the replay page. */
  readonly session: WebSessionState | null;
  readonly replayFailure: string | null;
  readonly onReattach: () => void;
  readonly onReconnectControls: () => void;
  readonly onRetryReplay: () => void;
  /** Null for replay, which has no command channel by contract. */
  readonly onCommand: ((input: RunCommand) => Promise<RunCommandOutcome>) | null;
}

export function RunPage({
  run,
  iterations,
  session,
  replayFailure,
  headerAction,
  ...events
}: RunPageProps): JSX.Element {
  const [requestedIterationId, setRequestedIterationId] = useState<string | null>(null);
  const banners = connectionBanners(run, session ?? REPLAY_SESSION);
  const header = pageHeader(run, session);
  const roots = iterationRoots(iterations, run);
  const selectedIterationId = roots.some(root => root.id === requestedIterationId)
    ? requestedIterationId
    : null;
  const selectedRound = compatibilityRoundKey(run, selectedIterationId);
  const transcript =
    selectedIterationId === null || selectedRound === null
      ? run.transcript
      : run.transcript.filter(entry => sameRoundKey(entry.roundKey ?? null, selectedRound));
  return (
    <main className="shell">
      <header className="header">
        <div>
          <p className="eyebrow">VIBESYS / RUN VIEWER</p>
          <h1>{header.heading}</h1>
        </div>
        <div className="run-header-actions">
          <span className={`status ${header.status.className}`}>{header.status.label}</span>
          {headerAction}
        </div>
      </header>
      <Banners banners={banners} replayFailure={replayFailure} {...events} />
      <RunSummary run={run} iterations={iterations} />
      <div className="run-layout">
        <RoundRail
          run={run}
          roots={roots}
          selectedId={selectedIterationId}
          onSelect={setRequestedIterationId}
        />
        <div className="run-main">
          <RunControls run={run} command={events.onCommand} />
          <section className="panel">
            <div className="panel-heading">
              <h2>Activity</h2>
              <span>{transcript.length} folded events</span>
            </div>
            <Transcript
              entries={transcript}
              emptyCopy={
                selectedRound === null
                  ? emptyTranscriptCopy(run, banners)
                  : 'No activity has been recorded for this iteration.'
              }
            />
          </section>
        </div>
      </div>
    </main>
  );
}

/** Resolve only compatibility roots to a transcript round; generic policy roots remain selectable
 * without inventing a relationship the iteration contract does not declare. */
function compatibilityRoundKey(run: CoreState, iterationId: string | null): RoundKey | null {
  if (iterationId === null) return null;
  const index = iterationRootsForCore(run).findIndex(root => root.id === iterationId);
  return index < 0 ? null : (roundsWithPlan(run.rounds, run.maxRounds)[index]?.key ?? null);
}
