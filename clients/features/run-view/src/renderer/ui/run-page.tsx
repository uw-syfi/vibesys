/**
 * The run page: header, connectivity banners, summary, and activity. Presentational: it renders
 * the run and the session it is given and reports clicks through its callbacks.
 */

import type {WebSessionState} from '@vibesys/run-session';
import type {CoreState} from '@vibesys/run-state';
import type {JSX} from 'react';
import {
  connectionBanners,
  emptyTranscriptCopy,
  pageHeader,
  REPLAY_SESSION,
} from '../../state/banners.js';
import {Banners} from './banners.js';
import {Activity, RunSummary} from './run-summary.js';

export interface RunPageProps {
  readonly run: CoreState;
  /** The live session's state; null on the replay page. */
  readonly session: WebSessionState | null;
  readonly replayFailure: string | null;
  readonly onReattach: () => void;
  readonly onReconnectControls: () => void;
  readonly onRetryReplay: () => void;
}

export function RunPage({run, session, replayFailure, ...events}: RunPageProps): JSX.Element {
  const banners = connectionBanners(run, session ?? REPLAY_SESSION);
  const header = pageHeader(run, session);
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
      <Activity run={run} emptyCopy={emptyTranscriptCopy(run, banners)} />
    </main>
  );
}
