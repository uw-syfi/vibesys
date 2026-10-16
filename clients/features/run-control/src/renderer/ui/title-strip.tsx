/**
 * The title strip: the attached run's host and link, and the controls its state allows
 * (`stripControls`). While a run fills the window, only this strip of the welcome view shows.
 */

import type {ChromeState} from '@vibesys/ipc';
import type {ResumeRunId} from '@vibesys/runs';
import {Button, StatusChip, type Tone} from '@vibesys/ui/legacy';
import type {JSX} from 'react';
import type {RunCommand} from '../../state/run-control.js';
import {
  connectionText,
  type RunLink,
  runLink,
  type StripControls,
  stripControls,
} from '../../state/strip.js';

/** Send a run command (attach, resume, stop, ...) to the run-control store. */
export type Dispatch = (command: RunCommand) => void;

type Attached = NonNullable<ChromeState['attached']>;

const TONES: Readonly<Record<RunLink, Tone>> = {
  connecting: 'warn',
  connected: 'ok',
  reconnecting: 'warn',
  stuck: 'error',
  ended: 'idle',
};

/** The buttons on the right of the strip, for an attached run. */
function RunButtons(props: {
  readonly attached: Attached;
  readonly controls: StripControls;
  readonly dispatch: Dispatch;
  readonly inspect: (attached: Attached) => void;
}): JSX.Element {
  const {attached, controls, dispatch, inspect} = props;
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
      <Button
        onClick={() => {
          if (!controls.back) dispatch({kind: 'show-welcome'});
          inspect(attached);
        }}
      >
        Inspect
      </Button>
      {controls.stop ? <Button onClick={() => stop(false)}>Stop run</Button> : null}
      {controls.force ? <Button onClick={() => stop(true)}>Force stop</Button> : null}
      {decision?.kind === 'available' ? (
        <Button variant="primary" onClick={() => resume(decision.runId)}>
          Resume…
        </Button>
      ) : null}
      {decision?.kind === 'unavailable' ? (
        <span id="strip-resume-state" className="muted" title={decision.reason}>
          {decision.reason}
        </span>
      ) : null}
      {controls.retry ? <Button onClick={() => dispatch({kind: 'retry'})}>Retry</Button> : null}
      {controls.back ? (
        <Button onClick={() => dispatch({kind: 'show-run'})}>Back to run</Button>
      ) : null}
    </>
  );
}

export function TitleStrip(props: {
  readonly chrome: ChromeState;
  readonly dispatch: Dispatch;
  readonly inspect: (attached: Attached) => void;
}): JSX.Element {
  const {chrome, dispatch, inspect} = props;
  const attached = chrome.attached;
  const controls = stripControls(chrome);
  const conn = attached === null ? '' : connectionText(attached);
  const inRun = chrome.mode === 'run';
  return (
    <header id="title-strip" tabIndex={-1}>
      <span className="brand">VibeSys</span>
      {attached === null ? null : (
        <StatusChip
          tone={TONES[runLink(attached.connection)]}
          label={attached.hostLabel}
          title={inRun ? 'Show hosts and runs' : `Attached to a run on ${attached.hostLabel}`}
          press={() => dispatch({kind: inRun ? 'show-welcome' : 'show-run'})}
        />
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
        <RunButtons attached={attached} controls={controls} dispatch={dispatch} inspect={inspect} />
      )}
    </header>
  );
}
