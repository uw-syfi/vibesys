/**
 * The title strip: the attached run's host and link, and the controls its state allows
 * (`stripControls`). While a run fills the window, only this strip of the welcome view shows.
 */
import type {JSX} from 'react';
import {type StripControls, stripControls} from '../../welcome-model.js';
import type {ChromeState} from '../../welcome-protocol.js';
import type {Dispatch} from './common.js';

type Attached = NonNullable<ChromeState['attached']>;

const DOTS: Readonly<Record<Attached['link'], string>> = {
  connecting: 'dot warn',
  connected: 'dot ok',
  reconnecting: 'dot warn',
  stuck: 'dot error',
  ended: 'dot',
};

function connection(attached: Attached | null): string {
  if (attached === null) return '';
  return attached.detail === '' ? attached.status : `${attached.status}: ${attached.detail}`;
}

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
  const resume = (): void => {
    dispatch({kind: 'show-welcome'});
    const run = attached.runId ?? '';
    dispatch({kind: 'resume', host: attached.host, project: attached.project, run});
  };
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
      {controls.resume ? (
        <button type="button" className="primary" onClick={resume}>
          Resume…
        </button>
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
  const conn = connection(attached);
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
          <span className={DOTS[attached.link]} />
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
