/**
 * The run page: header, connectivity banners, summary, and activity. Presentational: it renders
 * the run and the session it is given and reports clicks through its callbacks.
 */

import type {RunCommand, RunCommandOutcome, WebSessionState} from '@vibesys/run-session';
import type {CoreState} from '@vibesys/run-state';
import {type RoundKey, roundsWithPlan, sameRoundKey} from '@vibesys/run-state/rounds';
import {type JSX, useState} from 'react';
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
  readonly run: CoreState;
  /** The live session's state; null on the replay page. */
  readonly session: WebSessionState | null;
  readonly replayFailure: string | null;
  readonly onReattach: () => void;
  readonly onReconnectControls: () => void;
  readonly onRetryReplay: () => void;
  /** Null for replay, which has no command channel by contract. */
  readonly onCommand: ((input: RunCommand) => Promise<RunCommandOutcome>) | null;
}

export function RunPage({run, session, replayFailure, ...events}: RunPageProps): JSX.Element {
  const [requestedRound, setRequestedRound] = useState<RoundKey | null>(null);
  const banners = connectionBanners(run, session ?? REPLAY_SESSION);
  const header = pageHeader(run, session);
  const availableRounds = roundsWithPlan(run.rounds, run.maxRounds);
  const selectedRound =
    requestedRound !== null &&
    availableRounds.some(round => sameRoundKey(round.key, requestedRound))
      ? requestedRound
      : null;
  const transcript =
    selectedRound === null
      ? run.transcript
      : run.transcript.filter(entry => sameRoundKey(entry.roundKey ?? null, selectedRound));
  return (
    <main className="shell">
      <header className="header">
        <div>
          <p className="eyebrow">VIBESYS / RUN VIEWER</p>
          <h1>{header.heading}</h1>
        </div>
        <span className={`status ${header.status.className}`}>{header.status.label}</span>
      </header>
      <Banners banners={banners} replayFailure={replayFailure} {...events} />
      <RunSummary run={run} />
      <div className="run-layout">
        <RoundRail run={run} selected={selectedRound} onSelect={setRequestedRound} />
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
                  : 'No activity has been recorded for this round.'
              }
            />
          </section>
        </div>
      </div>
    </main>
  );
}
