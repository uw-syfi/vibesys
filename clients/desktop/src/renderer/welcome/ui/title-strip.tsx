/**
 * The title strip: the attached run's host and link, and the controls its state allows
 * (`stripControls`). While a run fills the window, only this strip of the welcome view shows.
 */
import type {JSX} from 'react';
import type {ResumeRunId} from '../../../common/resume-run.js';
import type {ChromeState} from '../../../common/welcome-protocol.js';
import {
  connectionText,
  type RunLink,
  runLink,
  type StripControls,
  stripControls,
} from '../state/strip.js';
import type {Dispatch} from './common.js';

type Attached = NonNullable<ChromeState['attached']>;

const DOTS: Readonly<Record<RunLink, string>> = {
  connecting: 'dot warn',
  connected: 'dot ok',
  reconnecting: 'dot warn',
  stuck: 'dot error',
  ended: 'dot',
};

/** The buttons on the right of the strip, for an attached run. */
function RunButtons(props: {
  readonly attached: Attached;
  readonly controls: StripControls;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {attached, controls, dispatch} = props;
  const instance = attached.instanceId;
  const stop = (force: boolean): void => {
    if (instance !== null) dispatch({kind: 'stop', host: attached.host, instance, force});
  };
  const resume = (run: ResumeRunId): void => {
    dispatch({kind: 'show-welcome'});
    dispatch({kind: 'resume', host: attached.host, project: attached.project, run});
  };
  const decision = controls.resume;
  return (
    <>
      {controls.stop ? (
        <button type="button" onClick={() => stop(false)}>
          Stop run
        </button>
      ) : null}
      {controls.force ? (
        <button type="button" onClick={() => stop(true)}>
          Force stop
        </button>
      ) : null}
      {decision?.kind === 'available' ? (
        <button type="button" className="primary" onClick={() => resume(decision.runId)}>
          Resume…
        </button>
      ) : null}
      {decision?.kind === 'unavailable' ? (
        <span id="strip-resume-state" className="muted" title={decision.reason}>
          {decision.reason}
        </span>
      ) : null}
      {controls.retry ? (
        <button type="button" onClick={() => dispatch({kind: 'retry'})}>
          Retry
        </button>
      ) : null}
      {controls.back ? (
        <button type="button" onClick={() => dispatch({kind: 'show-run'})}>
          Back to run
        </button>
      ) : null}
    </>
  );
}

export function TitleStrip(props: {
  readonly chrome: ChromeState;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {chrome, dispatch} = props;
  const attached = chrome.attached;
  const controls = stripControls(chrome);
  const conn = attached === null ? '' : connectionText(attached);
  const inRun = chrome.mode === 'run';
  return (
    <header>
      <span className="brand">VibeSys</span>
      {attached === null ? null : (
        <button
          className="chip"
          type="button"
          title={inRun ? 'Show hosts and runs' : `Attached to a run on ${attached.hostLabel}`}
          onClick={() => dispatch({kind: inRun ? 'show-welcome' : 'show-run'})}
        >
          <span className={DOTS[runLink(attached.connection)]} />
          {attached.hostLabel}
        </button>
      )}
      <span id="conn" className={controls.retry ? 'error' : 'muted'} title={conn}>
        {conn}
      </span>
      <span
        id="strip-stop-state"
        className={controls.stopError ? 'error' : 'muted'}
        title={controls.stopText}
      >
        {controls.stopText}
      </span>
      <span className="grow" />
      {attached === null ? null : (
        <RunButtons attached={attached} controls={controls} dispatch={dispatch} />
      )}
    </header>
  );
}
