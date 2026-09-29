/** Reopening a finished run read-only, and resuming one with its recorded configuration. */
import {useCallback, useEffect, useState} from 'react';
import type {Catalog, HomeClient} from './home-api.js';
import {launchLine, useLaunch} from './launch.js';
import {homeHref} from './route.js';
import {budgetLabel} from './setup.js';
import {Hint, Row} from './ui/Setup.js';
import {StartFailure} from './ui/StartFailure.js';
import {Titlebar} from './ui/TitleRow.js';

export interface RunEntryProps {
  client: HomeClient;
  token: string;
  projectId: string;
  runId: string;
  title: string;
  root: string | null;
  /** The run's outer loop, matched against the catalog for the budget field's wording. */
  loop: string | null;
  /** The run's recorded budget total, the least a resume accepts; null when unknown. */
  recorded: number | null;
}

const WHOLE = /^[1-9]\d*$/;

function DidNotStart({title}: {title: string}) {
  return (
    <Titlebar>
      <span className="name">{title}</span>
      <span className="sp" />
      <span className="status">
        <span className="dot err" />
        Did not start
      </span>
    </Titlebar>
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
      <Titlebar>
        <span className="name">{title}</span>
      </Titlebar>
      {error === null ? (
        <p className="empty">
          <span className="busy">
            <span className="spin" />
            Opening the run read-only…
          </span>
        </p>
      ) : (
        <p className="empty bad" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

export function ResumeView(props: RunEntryProps) {
  const {client, token, projectId, runId, title, root, loop, recorded} = props;
  const {state, start, reset} = useLaunch(client, token);
  const [budget, setBudget] = useState('');
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  useEffect(() => {
    let live = true;
    client.catalog().then(
      next => {
        if (live) setCatalog(next);
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [client]);
  const label = budgetLabel(catalog?.outer_loops.find(option => option.id === loop));
  const trimmed = budget.trim();
  const valid = trimmed === '' || WHOLE.test(trimmed);
  const unit = label.toLowerCase();
  const resume = () =>
    start(projectId, () =>
      client.resume(projectId, runId, trimmed === '' ? null : Number(trimmed)),
    );
  if (state.kind === 'failed') {
    return (
      <>
        <DidNotStart title={title} />
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
      <Titlebar>
        <span className="name">{title}</span>
        <span className="sp" />
        <span className="status">Resume</span>
      </Titlebar>
      <div className="scroll">
        <div className="form">
          <Row label={label} htmlFor="f-budget">
            <input
              id="f-budget"
              className={
                line.error === null ? 'fld num budget resume' : 'fld num budget resume bad'
              }
              type="number"
              min={recorded ?? 1}
              step={1}
              inputMode="numeric"
              value={budget}
              placeholder="Recorded"
              title={`Total ${unit} for the run; empty keeps the recorded total`}
              onChange={event => setBudget(event.target.value)}
            />
            <Hint tone={line.error === null ? 'plain' : 'bad'}>
              {line.error ??
                (recorded === null
                  ? `Runs again with the recorded configuration and total. A larger total adds ${unit}.`
                  : `Recorded: ${recorded} ${unit}. Runs again with the recorded configuration; a larger total adds ${unit}.`)}
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
