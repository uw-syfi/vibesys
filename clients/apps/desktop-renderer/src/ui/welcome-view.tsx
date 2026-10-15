/**
 * The welcome view: the title strip, Recent, one host's panel, "Connect to host", and the two
 * sheets, each a feature's view. Composition: it renders each store's state, and wires the flows
 * that cross features (a listed run's attach and stop go to run control, "Start a run…" opens the
 * start sheet with the projects the host and Recent suggest, the project field completes paths on
 * the host). While a run fills the window only the title strip shows, and the sheets wait closed.
 */

import {HostPanel, HostPicker, InspectorSheet, PathInput} from '@vibesys/feature-hosts/renderer';
import type {
  CompletionAction,
  CompletionState,
  HostAction,
  HostState,
  InspectorSheetAction,
  InspectorSheetState,
  PickerAction,
  PickerState,
} from '@vibesys/feature-hosts/state';
import {RecentPanel} from '@vibesys/feature-recent-runs/renderer';
import type {ListsState} from '@vibesys/feature-recent-runs/state';
import {type Dispatch, StopControls, TitleStrip} from '@vibesys/feature-run-control/renderer';
import type {WelcomeAction, WelcomeState} from '@vibesys/feature-run-control/state';
import {type ProjectField, StartSheet} from '@vibesys/feature-start-run/renderer';
import type {StartAction, StartState} from '@vibesys/feature-start-run/state';
import type {HostKey, HostRun} from '@vibesys/hosts';
import {type RecentRun, stopKey, suggestProjects} from '@vibesys/runs';
import {Button} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {WORDMARK} from './welcome-banner.js';

/** Where the view's events go: one dispatcher per store. */
interface WelcomeSenders {
  readonly welcome: (action: WelcomeAction) => void;
  readonly host: (action: HostAction) => void;
  readonly start: (action: StartAction) => void;
  readonly picker: (action: PickerAction) => void;
  readonly completion: (action: CompletionAction) => void;
  readonly inspector: (action: InspectorSheetAction) => void;
}

type Attached = NonNullable<WelcomeState['chrome']['attached']>;

function inspectAttached(inspect: WelcomeSenders['inspector'], attached: Attached): void {
  inspect({
    type: 'open',
    target: {
      host: attached.host,
      hostLabel: attached.hostLabel,
      instanceId: attached.instanceId,
      task: null,
      runId: attached.runId,
      project: attached.project,
      focusFallbackId: 'title-strip',
    },
  });
}

/** The view's whole input: each store's state, and where its events go. */
export interface WelcomeViewProps {
  readonly welcome: WelcomeState;
  readonly lists: ListsState;
  readonly host: HostState;
  readonly start: StartState;
  readonly picker: PickerState;
  readonly completion: CompletionState;
  readonly inspector: InspectorSheetState;
  readonly send: WelcomeSenders;
}

export function WelcomeView(props: WelcomeViewProps): JSX.Element {
  const {
    welcome: {chrome, banner},
    lists,
    completion,
    send,
  } = props;
  const visible = chrome.mode === 'welcome';
  const dispatch: Dispatch = command => send.welcome({type: 'command', command});
  const stopControls = (host: HostKey, instance: string): ReactNode => (
    <StopControls
      view={chrome.stops[stopKey(host, instance)]}
      stop={force => dispatch({kind: 'stop', host, instance, force})}
    />
  );
  const attach = (host: HostKey, instance: string): void =>
    dispatch({kind: 'attach', host, instance});
  const inspectHostRun = (host: HostKey, hostLabel: string, run: HostRun): void =>
    send.inspector({
      type: 'open',
      target: {
        host,
        hostLabel,
        instanceId: run.id,
        task: null,
        runId: run.runId,
        project: run.projectRoot,
        focusFallbackId: 'host-panel',
      },
    });
  const inspectRecent = (run: RecentRun, instanceId: string): void =>
    send.inspector({
      type: 'open',
      target: {
        host: run.host,
        hostLabel: run.hostLabel,
        instanceId,
        task: run.task,
        runId: run.runId,
        project: run.project,
        focusFallbackId: 'recent-section',
      },
    });
  const shownHost = props.host.host;
  const projectField: ProjectField = field => (
    <PathInput {...field} field="project" completion={completion} complete={send.completion} />
  );
  return (
    <div className={visible ? 'view' : 'view strip'}>
      <TitleStrip
        chrome={chrome}
        dispatch={dispatch}
        inspect={attached => inspectAttached(send.inspector, attached)}
      />
      <main>
        <div className="column">
          <div className="hero">
            <pre role="img" aria-label="VibeSys">
              {WORDMARK}
            </pre>
            <p className="muted">
              Attach to a run, or start a new one on this Mac or another host.
            </p>
          </div>
          <p className={banner?.error === true ? 'status error' : 'status'} role="status">
            {banner?.text ?? ''}
          </p>
          <RecentPanel
            lists={lists}
            actions={{
              attach,
              resume: (host, project, run) => dispatch({kind: 'resume', host, project, run}),
              stopControls,
              openHost: host => send.host({type: 'show', host}),
              inspect: inspectRecent,
            }}
          />
          <HostPanel
            state={props.host}
            listing={lists.listings[shownHost]}
            completion={completion}
            actions={{attach, stopControls, inspect: inspectHostRun}}
            send={{
              host: send.host,
              completion: send.completion,
              startRun: host =>
                send.start({
                  type: 'open',
                  host,
                  projects: suggestProjects(lists.listings[host.key], lists.runs, host.key),
                }),
            }}
          />
          <section>
            <h2>Other hosts</h2>
            <div className="row">
              <Button onClick={() => send.picker({type: 'open'})}>Connect to host…</Button>
              <span className="muted">Hosts from ~/.ssh/config, or type user@host.</span>
            </div>
          </section>
        </div>
      </main>
      <StartSheet
        state={props.start}
        visible={visible}
        projectField={projectField}
        start={send.start}
      />
      <HostPicker state={props.picker} visible={visible} picker={send.picker} />
      <InspectorSheet state={props.inspector} send={send.inspector} />
    </div>
  );
}
