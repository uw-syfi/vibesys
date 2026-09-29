/** Reopening a finished run read-only, and resuming one with its recorded configuration. */
import {useCallback, useEffect, useState} from 'react';
import type {HomeClient} from './home-api.js';
import {launchLine, useLaunch} from './launch.js';
import {homeHref} from './route.js';
import {Hint, Row} from './ui/Setup.js';
import {StartFailure} from './ui/StartFailure.js';

export interface RunEntryProps {
  client: HomeClient;
  token: string;
  projectId: string;
  runId: string;
  title: string;
  root: string | null;
}

const WHOLE = /^[1-9]\d*$/;

function DidNotStart({title}: {title: string}) {
  return (
    <header className="titlebar">
      <span className="name">{title}</span>
      <span className="sp" />
      <span className="status">
        <span className="dot err" />
        Did not start
      </span>
    </header>
  );
}

export function ReopenView({client, token, projectId, runId, title, root}: RunEntryProps) {
  const {state, start} = useLaunch(client, token);
  const open = useCallback(
    () => start(projectId, () => client.open(projectId, runId)),
    [start, client, projectId, runId],
  );
  useEffect(open, [open]);
  if (state.kind === 'failed') {
    return (
      <>
        <DidNotStart title={title} />
        <StartFailure
          failure={state.failure}
          root={root}
          backLabel="Back"
          onRetry={open}
          onBack={() => window.location.assign(homeHref(token, {kind: 'empty'}))}
        />
      </>
    );
  }
  const {error} = launchLine(state);
  return (
    <>
      <header className="titlebar">
        <span className="name">{title}</span>
      </header>
      <p
        className={error === null ? 'empty' : 'empty bad'}
        role={error === null ? undefined : 'alert'}
      >
        {error ?? 'Opening the run read-only…'}
      </p>
    </>
  );
}

export function ResumeView({client, token, projectId, runId, title, root}: RunEntryProps) {
  const {state, start, reset} = useLaunch(client, token);
  const [budget, setBudget] = useState('');
  const trimmed = budget.trim();
  const valid = trimmed === '' || WHOLE.test(trimmed);
  const resume = () =>
    start(projectId, () =>
      client.resume(projectId, runId, trimmed === '' ? null : Number(trimmed)),
    );
  const heading = `Resume ${title}`;
  if (state.kind === 'failed') {
    return (
      <>
        <DidNotStart title={heading} />
        <StartFailure
          failure={state.failure}
          root={root}
          backLabel="Back"
          onRetry={resume}
          onBack={reset}
        />
      </>
    );
  }
  const line = launchLine(state);
  return (
    <>
      <header className="titlebar">
        <span className="name">{heading}</span>
      </header>
      <div className="scroll">
        <div className="form">
          <Row label="Budget" htmlFor="f-budget">
            <input
              id="f-budget"
              className={line.error === null ? 'fld num budget' : 'fld num budget bad'}
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={budget}
              placeholder="As recorded"
              title="Total rounds (or generations) for the run; only a larger total adds any"
              onChange={event => setBudget(event.target.value)}
            />
            <Hint tone={line.error === null ? 'plain' : 'bad'}>
              {line.error ??
                'Runs again with the recorded configuration. A larger total adds rounds.'}
            </Hint>
          </Row>
        </div>
      </div>
      <footer className="sheetfoot">
        {line.busy === null ? null : (
          <span className="busy" aria-live="polite">
            <span className="spin" />
            {line.busy}
          </span>
        )}
        <span className="sp" />
        <a className="btn ghost" href={homeHref(token, {kind: 'empty'})}>
          Cancel
        </a>
        <button
          type="button"
          className="btn primary"
          disabled={!valid || line.busy !== null}
          onClick={resume}
        >
          Resume run
        </button>
      </footer>
    </>
  );
}
