import {type HostKey, hostKey, hostLabel} from '@vibesys/hosts';
import type {MachineClock} from '@vibesys/machine';
import {Page, pageState, Stack, Text} from '@vibesys/ui';
import {type JSX, useReducer} from 'react';
import type {HostActor, HostView, NewBackoff} from '../state/index.js';
import {AgentPanel} from './agent-panel.js';
import {ConnectionBanner, type ConnectionNote} from './connection-banner.js';
import {type HostConnection, useHosts} from './hosts.js';
import type {IdeasLoad} from './ideas-view.js';
import {agentNames, nameOf} from './names.js';
import {initialRoute, type RunView, routeReducer} from './route.js';
import {RunList} from './run-list.js';
import {RunScreen} from './run-screen.js';

const TITLE = 'Orchestrated runs';

/** What a host's run stream says about the connection, with the Retry that dials again. */
function runsNote(view: HostView, actor: HostActor): ConnectionNote {
  const name = hostLabel(view.context.host);
  return {
    key: `runs/${hostKey(view.context.host)}`,
    what: `the runs on ${name}`,
    status: view.runs,
    fault: view.context.runsFault,
    hasData: view.context.state.sequence > 0n,
    retry: () => actor.send({type: 'retry'}),
  };
}

function agentNote(view: HostView, actor: HostActor, name: string): ConnectionNote | null {
  const agent = view.agent;
  if (agent === null) return null;
  return {
    key: `agent/${agent.agentId}`,
    what: `the steps of ${name}`,
    status: agent.stream,
    fault: agent.fault,
    hasData: Object.values(view.context.state.items).some(item => item.agentId === agent.agentId),
    retry: () => actor.send({type: 'agent.retry', agentId: agent.agentId}),
  };
}

/** The orchestrator window: every connected host's runs, and one run open at a time. */
export function OrchestratorApp(props: {
  readonly connections: readonly HostConnection[];
  /** Milliseconds since the epoch; the caller owns the clock. */
  readonly now: () => number;
  /** A new id for each message sent (a retry of one message reuses its id). */
  readonly newId: () => string;
  /** The redial schedule a lost connection follows: a fresh `BackoffSchedule` per call. */
  readonly backoff: NewBackoff;
  /** The scheduler the machines' deadlines and redials run on; the browser's timers by default. */
  readonly clock?: MachineClock;
}): JSX.Element {
  const [route, go] = useReducer(routeReducer, initialRoute);
  const {actors, views} = useHosts(props.connections, props.backoff, props.clock);
  const loading = views.length === 0;
  if (route.screen === 'runs') {
    const notes = views.flatMap(view => {
      const actor = actors.get(hostKey(view.context.host));
      return actor === undefined ? [] : [runsNote(view, actor)];
    });
    return (
      <Page width="wide" state={pageState(loading)} header={<Text variant="title">{TITLE}</Text>}>
        <Stack gap="space-4">
          <ConnectionBanner notes={notes} />
          <RunList
            hosts={views.map(view => ({
              key: hostKey(view.context.host),
              label: hostLabel(view.context.host),
              state: view.context.state,
            }))}
            open={(host, runId) => go({type: 'open-run', host, runId})}
          />
        </Stack>
      </Page>
    );
  }
  const actor = actors.get(route.host as HostKey);
  const view = views.find(candidate => hostKey(candidate.context.host) === route.host);
  const {runId} = route;
  if (actor === undefined || view === undefined) {
    return (
      <Page state="loading" header={<Text variant="title">{TITLE}</Text>}>
        <Text tone="muted">Connecting.</Text>
      </Page>
    );
  }
  const {state} = view.context;
  const agent = route.agentId === null ? undefined : state.agents[route.agentId];
  const show = (next: RunView): void => {
    go({type: 'show', view: next});
    if (next === 'ideas' || next === 'handoffs') actor.send({type: 'plans.load', runId});
  };
  const plans = view.context.plans;
  const ideas: IdeasLoad =
    plans.status === 'ready' || plans.status === 'failed' || plans.status === 'loading'
      ? plans
      : {status: 'idle'};
  const names = agentNames(state, runId);
  const notes = [
    runsNote(view, actor),
    ...(agent === undefined ? [] : [agentNote(view, actor, nameOf(names, agent))]),
  ].filter((note): note is ConnectionNote => note !== null);
  const agentView = view.agent !== null && view.agent.agentId === route.agentId ? view.agent : null;
  return (
    <Page width="fill" state="ready" header={<Text variant="title">{TITLE}</Text>}>
      <Stack gap="space-4">
        <ConnectionBanner notes={notes} />
        <RunScreen
          state={state}
          host={hostLabel(view.context.host)}
          runId={runId}
          nowMs={props.now()}
          view={route.view}
          agentId={route.agentId}
          plans={plans.status === 'ready' ? plans.plans : []}
          ideas={ideas}
          goRuns={() => go({type: 'close-run'})}
          show={show}
          pick={agentId => {
            if (route.agentId !== null && route.agentId !== agentId) {
              actor.send({type: 'agent.close', agentId: route.agentId});
            }
            go({type: 'pick-agent', agentId});
            actor.send({type: 'agent.open', agentId});
          }}
          agentPanel={
            agent === undefined || agentView === null ? null : (
              <AgentPanel
                state={state}
                agent={agent}
                name={nameOf(names, agent)}
                view={agentView}
                showOlder={() => actor.send({type: 'agent.older', agentId: agent.id})}
                send={text =>
                  actor.send({
                    type: 'message.send',
                    commandId: props.newId(),
                    agentId: agent.id,
                    text,
                  })
                }
                dismiss={() => actor.send({type: 'message.dismiss', agentId: agent.id})}
                close={() => {
                  go({type: 'close-agent'});
                  actor.send({type: 'agent.close', agentId: agent.id});
                }}
              />
            )
          }
        />
      </Stack>
    </Page>
  );
}
