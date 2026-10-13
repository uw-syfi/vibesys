/**
 * The host panel: one host's VibeSys checkout (or the form asking for it), its runs as the host
 * pushes them, and "Start a run…"; or why the host cannot be reached, with Sign in.
 */

import type {HostKey, HostListing, HostRun, HostSummary} from '@vibesys/hosts';
import {Button} from '@vibesys/ui';
import type {FormEvent, JSX, KeyboardEvent, ReactNode} from 'react';
import type {CompletionAction, CompletionState} from '../../state/completion.js';
import {type CheckoutForm, type HostAction, type HostState, hostLabelOf} from '../../state/host.js';
import {PathInput} from './path-input.js';

/**
 * What a listed run offers, which other features own: attaching to it, and its stop controls. The
 * app composes them in.
 */
export interface HostRunActions {
  readonly attach: (host: HostKey, instance: string) => void;
  readonly stopControls: (host: HostKey, instance: string) => ReactNode;
  readonly inspect: (host: HostKey, hostLabel: string, run: HostRun) => void;
}

/** Where the panel's events go. */
export interface HostPanelSenders {
  readonly host: (action: HostAction) => void;
  readonly completion: (action: CompletionAction) => void;
  /** Open "Start a run" on the shown host (another feature's sheet). */
  readonly startRun: (host: {readonly key: HostKey; readonly label: string}) => void;
}

/** `run`'s status cell text: its phase, with the failure reason appended when it has one. */
function runStatusText(run: HostRun): string {
  return run.reason === null ? run.status : `${run.status}: ${run.reason}`;
}

/** `run`'s started-at cell text, blank for a run that has not reported starting yet. */
function runStartedText(run: HostRun): string {
  return run.startedAt === 0 ? '' : new Date(run.startedAt * 1000).toLocaleString();
}

function RunRow(props: {
  readonly run: HostRun;
  readonly host: HostKey;
  readonly hostLabel: string;
  readonly actions: HostRunActions;
}): JSX.Element {
  const {run, host, hostLabel, actions} = props;
  return (
    <tr>
      <td>{run.runId}</td>
      <td className="path">{run.projectRoot}</td>
      <td>{runStatusText(run)}</td>
      <td>{runStartedText(run)}</td>
      <td className="actions-cell">
        <Button variant="primary" onClick={() => actions.attach(host, run.id)}>
          Attach
        </Button>
        {actions.stopControls(host, run.id)}
        <Button onClick={() => actions.inspect(host, hostLabel, run)}>Inspect</Button>
      </td>
    </tr>
  );
}

/** One of a host's ended runs (its registry tombstone): identity and outcome, no live actions. */
function EndedRunRow(props: {readonly run: HostRun}): JSX.Element {
  const {run} = props;
  return (
    <tr>
      <td>{run.runId}</td>
      <td className="path">{run.projectRoot}</td>
      <td>{runStatusText(run)}</td>
      <td>{runStartedText(run)}</td>
    </tr>
  );
}

function RunTable(props: {
  readonly data: HostSummary;
  readonly listing: HostListing | undefined;
  readonly actions: HostRunActions;
  readonly send: HostPanelSenders;
}): JSX.Element {
  const {data, listing, actions, send} = props;
  if (listing === undefined) return <p className="muted">Connecting to {data.label}…</p>;
  if (listing.kind === 'unavailable') return <Unavailable listing={listing} send={send} />;
  const unreadable =
    listing.unreadable.length === 0 ? null : (
      <p className="warn">
        {listing.unreadable.length} run(s) on {data.label} belong to another VibeSys version.
      </p>
    );
  const unverified =
    listing.unverified.length === 0 ? null : (
      <p className="muted">
        {listing.unverified.length} run(s) on {data.label} could not be confirmed (their lock could
        not be read).
      </p>
    );
  const runs =
    listing.live.length === 0 ? (
      <p className="muted">No live runs on {data.label}.</p>
    ) : (
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Project</th>
            <th>Status</th>
            <th>Started</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {listing.live.map(run => (
            <RunRow
              key={run.id}
              run={run}
              host={data.key}
              hostLabel={data.label}
              actions={actions}
            />
          ))}
        </tbody>
      </table>
    );
  const ended =
    listing.ended.length === 0 ? null : (
      <>
        <p className="muted">Ended runs on {data.label}:</p>
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Project</th>
              <th>Status</th>
              <th>Started</th>
            </tr>
          </thead>
          <tbody>
            {listing.ended.map(run => (
              <EndedRunRow key={run.id} run={run} />
            ))}
          </tbody>
        </table>
      </>
    );
  return (
    <>
      {runs}
      {ended}
      {unreadable}
      {unverified}
    </>
  );
}

/** Why the host's runs cannot be shown now: the connection's state, and Sign in when it asks. */
function Unavailable(props: {
  readonly listing: Extract<HostListing, {kind: 'unavailable'}>;
  readonly send: HostPanelSenders;
}): JSX.Element {
  const {connection} = props.listing;
  switch (connection.kind) {
    case 'auth-needed':
      return (
        <>
          <p className="error">{connection.detail}</p>
          <Button variant="primary" onClick={() => props.send.host({type: 'sign-in'})}>
            Sign in
          </Button>
        </>
      );
    case 'offline':
    case 'incompatible':
      return <p className="error">{connection.detail}</p>;
    case 'reconnecting':
      return <p className="muted">Reconnecting (attempt {connection.attempt})…</p>;
    default:
      return <p className="muted">Connecting…</p>;
  }
}

function formStatus(form: CheckoutForm, data: HostSummary): string {
  if (form.error !== null) return form.error;
  if (form.checking !== null) return `Checking ${form.value.trim()} on ${data.label}…`;
  return '';
}

/** "Where is your VibeSys checkout on HOST?", checked by the host before it is saved. */
function CheckoutQuestion(props: {
  readonly form: CheckoutForm;
  readonly data: HostSummary;
  readonly host: (action: HostAction) => void;
  readonly completion: CompletionState;
  readonly send: HostPanelSenders;
  readonly signIn: ReactNode;
}): JSX.Element {
  const {form, data, host} = props;
  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    host({type: 'submit-checkout'});
  };
  const onKeyDown = (event: KeyboardEvent): void => {
    if (event.key !== 'Escape' || !form.cancellable) return;
    event.stopPropagation();
    host({type: 'cancel-checkout'});
  };
  return (
    <form onSubmit={onSubmit} onKeyDown={onKeyDown}>
      <p>Where is your VibeSys checkout on {data.label}?</p>
      <p className="muted">
        The directory you cloned VibeSys into (its pyproject.toml declares the vibesys project). The
        app starts its host helper there with uv.
      </p>
      <div className="row">
        <PathInput
          autoFocus
          field="checkout"
          host={data.key}
          value={form.value}
          completion={props.completion}
          complete={props.send.completion}
          change={value => host({type: 'checkout-input', value})}
          placeholder="/home/me/src/vibesys"
        />
        <Button type="submit" variant="primary" disabled={form.checking !== null}>
          Check and save
        </Button>
        {form.cancellable ? (
          <Button onClick={() => host({type: 'cancel-checkout'})}>Cancel</Button>
        ) : null}
      </div>
      <p className={form.error === null ? 'status' : 'status error'}>{formStatus(form, data)}</p>
      {form.authNeeded ? props.signIn : null}
    </form>
  );
}

function Ready(props: {
  readonly panel: Extract<HostState['panel'], {kind: 'ready'}>;
  readonly listing: HostListing | undefined;
  readonly completion: CompletionState;
  readonly actions: HostRunActions;
  readonly send: HostPanelSenders;
  readonly signIn: ReactNode;
}): JSX.Element {
  const {panel, listing, actions, send} = props;
  const data = panel.data;
  const host = send.host;
  return (
    <>
      {panel.checkout === null ? (
        <div className="row">
          <span className="muted">VibeSys checkout:</span>
          <code>{data.checkout}</code>
          <Button variant="link" onClick={() => host({type: 'edit-checkout'})}>
            Change
          </Button>
        </div>
      ) : (
        <CheckoutQuestion
          form={panel.checkout}
          data={data}
          host={host}
          completion={props.completion}
          send={send}
          signIn={props.signIn}
        />
      )}
      {data.checkout === null ? null : (
        <>
          <RunTable data={data} listing={listing} actions={actions} send={send} />
          <div className="row">
            <Button
              variant="primary"
              onClick={() => send.startRun({key: data.key, label: data.label})}
            >
              Start a run…
            </Button>
          </div>
        </>
      )}
    </>
  );
}

function SignIn(props: {
  readonly state: HostState;
  readonly host: (action: HostAction) => void;
}): JSX.Element {
  const signIn = props.state.signIn;
  const label = hostLabelOf(props.state.host);
  let note: ReactNode = null;
  if (signIn !== null) {
    note =
      'error' in signIn ? (
        <p className="error">{signIn.error}</p>
      ) : (
        <p className="muted">Signing in to {label}…</p>
      );
  }
  return (
    <>
      {note}
      <Button variant="primary" onClick={() => props.host({type: 'sign-in'})}>
        Sign in to {label}
      </Button>
    </>
  );
}

export function HostPanel(props: {
  readonly state: HostState;
  /** The shown host's runs, as its helper last pushed them. */
  readonly listing: HostListing | undefined;
  readonly completion: CompletionState;
  readonly actions: HostRunActions;
  readonly send: HostPanelSenders;
}): JSX.Element {
  const {state, send} = props;
  const label = hostLabelOf(state.host);
  const panel = state.panel;
  const signIn = <SignIn state={state} host={send.host} />;
  let body: ReactNode;
  if (panel.kind === 'loading') {
    body = <p className="muted">Connecting to {label}…</p>;
  } else if (panel.kind === 'failed') {
    body = (
      <>
        <p className="error">{panel.error}</p>
        {panel.authNeeded ? (
          signIn
        ) : (
          <Button onClick={() => send.host({type: 'reload'})}>Try again</Button>
        )}
      </>
    );
  } else {
    body = (
      <Ready
        panel={panel}
        listing={props.listing}
        completion={props.completion}
        actions={props.actions}
        send={send}
        signIn={signIn}
      />
    );
  }
  return (
    <section id="host-panel" tabIndex={-1}>
      <h2>
        {label}
        {state.host === 'local' ? null : (
          <Button variant="link" onClick={() => send.host({type: 'show', host: 'local'})}>
            Back to This Mac
          </Button>
        )}
      </h2>
      {body}
    </section>
  );
}
