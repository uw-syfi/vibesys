/**
 * The host panel: one host's VibeSys checkout (or the form asking for it), its runs as the host
 * pushes them, and "Start a run…"; or why the host cannot be reached, with Sign in.
 */

import {
  type HostKey,
  type HostListing,
  type HostRun,
  type HostSummary,
  stopKey,
  suggestProjects,
} from '@vibesys/entities';
import type {ChromeState} from '@vibesys/ipc';
import {Button} from '@vibesys/ui';
import type {FormEvent, JSX, KeyboardEvent, ReactNode} from 'react';
import type {CompletionState} from '../state/completion.js';
import {type CheckoutForm, type HostAction, type HostState, hostLabelOf} from '../state/host.js';
import type {ListsState} from '../state/lists.js';
import {commands, PathInput, StopControls, type WelcomeSenders} from './common.js';

function RunRow(props: {
  readonly run: HostRun;
  readonly host: HostKey;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {run, host, chrome} = props;
  const dispatch = commands(props.send);
  return (
    <tr>
      <td>{run.runId}</td>
      <td className="path">{run.projectRoot}</td>
      <td>{run.reason === null ? run.status : `${run.status}: ${run.reason}`}</td>
      <td>{run.startedAt === 0 ? '' : new Date(run.startedAt * 1000).toLocaleString()}</td>
      <td className="actions-cell">
        <Button
          variant="primary"
          onClick={() => dispatch({kind: 'attach', host, instance: run.id})}
        >
          Attach
        </Button>
        <StopControls
          view={chrome.stops[stopKey(host, run.id)]}
          stop={force => dispatch({kind: 'stop', host, instance: run.id, force})}
        />
      </td>
    </tr>
  );
}

function RunTable(props: {
  readonly data: HostSummary;
  readonly listing: HostListing | undefined;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {data, listing, chrome, send} = props;
  if (listing === undefined) return <p className="muted">Connecting to {data.label}…</p>;
  if (listing.kind === 'unavailable') return <Unavailable listing={listing} send={send} />;
  const unreadable =
    listing.unreadable.length === 0 ? null : (
      <p className="warn">
        {listing.unreadable.length} run(s) on {data.label} belong to another VibeSys version.
      </p>
    );
  if (listing.live.length === 0) {
    return (
      <>
        <p className="muted">No live runs on {data.label}.</p>
        {unreadable}
      </>
    );
  }
  return (
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
          <RunRow key={run.id} run={run} host={data.key} chrome={chrome} send={send} />
        ))}
      </tbody>
    </table>
  );
}

/** Why the host's runs cannot be shown now: the connection's state, and Sign in when it asks. */
function Unavailable(props: {
  readonly listing: Extract<HostListing, {kind: 'unavailable'}>;
  readonly send: WelcomeSenders;
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
  readonly send: WelcomeSenders;
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
  readonly lists: ListsState;
  readonly completion: CompletionState;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
  readonly signIn: ReactNode;
}): JSX.Element {
  const {panel, lists, chrome, send} = props;
  const listing = lists.listings[panel.data.key];
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
          <RunTable data={data} listing={listing} chrome={chrome} send={send} />
          <div className="row">
            <Button
              variant="primary"
              onClick={() =>
                send.start({
                  type: 'open',
                  host: {key: data.key, label: data.label},
                  projects: suggestProjects(listing, lists.runs, data.key),
                })
              }
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
  readonly lists: ListsState;
  readonly completion: CompletionState;
  readonly chrome: ChromeState;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {state, chrome, send} = props;
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
        lists={props.lists}
        completion={props.completion}
        chrome={chrome}
        send={send}
        signIn={signIn}
      />
    );
  }
  return (
    <section id="host-panel">
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
