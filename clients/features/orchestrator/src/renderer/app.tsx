import {Page, pageState, Stack, Text} from '@vibesys/ui';
import {type JSX, useReducer, useRef} from 'react';
import {AgentPanel} from './agent-panel.js';
import {type HostConnection, useHosts} from './hosts.js';
import type {IdeasLoad} from './ideas-view.js';
import {agentNames, nameOf} from './names.js';
import {initialRoute, type RunView, routeReducer} from './route.js';
import {RunList} from './run-list.js';
import {RunScreen} from './run-screen.js';

/** The orchestrator window: every connected host's runs, and one run open at a time. */
export function OrchestratorApp(props: {
  readonly connections: readonly HostConnection[];
  /** Milliseconds since the epoch; the caller owns the clock. */
  readonly now: () => number;
}): JSX.Element {
  const [route, go] = useReducer(routeReducer, initialRoute);
  const {actors, views} = useHosts(props.connections);
  const commands = useRef(0);
  const hosts = views.map(view => ({
    host: view.context.host,
    connection: view.runs,
    state: view.context.state,
  }));
  const loading = views.length === 0;
  if (route.screen === 'runs') {
    return (
      <Page
        width="wide"
        state={pageState(loading)}
        header={<Text variant="title">Orchestrated runs</Text>}
      >
        <RunList hosts={hosts} open={(host, runId) => go({type: 'open-run', host, runId})} />
      </Page>
    );
  }
  const actor = actors.get(route.host);
  const view = views.find(candidate => candidate.context.host === route.host);
  const {runId} = route;
  if (actor === undefined || view === undefined) {
    return (
      <Page state="loading" header={<Text variant="title">Orchestrated runs</Text>}>
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
  return (
    <Page width="fill" state="ready" header={<Text variant="title">Orchestrated runs</Text>}>
      <Stack gap="space-4">
        <RunScreen
          state={state}
          host={route.host}
          runId={runId}
          nowMs={props.now()}
          view={route.view}
          agentId={route.agentId}
          plans={plans.status === 'ready' ? plans.plans : []}
          ideas={ideas}
          goRuns={() => go({type: 'close-run'})}
          show={show}
          pick={agentId => {
            go({type: 'pick-agent', agentId});
            actor.send({type: 'agent.open', agentId});
          }}
          agentPanel={
            agent === undefined ? null : (
              <AgentPanel
                state={state}
                agent={agent}
                name={nameOf(names, agent)}
                older={view.older}
                olderError={view.context.olderError}
                box={view.context.message}
                showOlder={() => actor.send({type: 'agent.older'})}
                send={text => {
                  commands.current += 1;
                  actor.send({
                    type: 'message.send',
                    commandId: `cmd-${route.host}-${commands.current}`,
                    agentId: agent.id,
                    text,
                  });
                }}
                dismiss={() => actor.send({type: 'message.dismiss'})}
                close={() => {
                  go({type: 'close-agent'});
                  actor.send({type: 'agent.close'});
                }}
              />
            )
          }
        />
      </Stack>
    </Page>
  );
}
