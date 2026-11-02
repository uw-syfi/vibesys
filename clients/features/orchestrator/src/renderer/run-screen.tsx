import type {Plan} from '@vibesys/orchestrator-proto';
import {agentTree, flattenTree, type OrchestratorState} from '@vibesys/orchestrator-state';
import {Breadcrumb, Card, Chip, Row, Split, Stack, Tabs, Text, Tree, TreeRow} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {CostView} from './cost-view.js';
import {HandoffCards} from './handoff-cards.js';
import {type IdeasLoad, IdeasView} from './ideas-view.js';
import {agentNames} from './names.js';
import type {RunView} from './route.js';
import {TimelineView} from './timeline-view.js';
import {agentWord, harnessWord, runWord} from './words.js';

const VIEWS = [
  {value: 'timeline', label: 'Timeline'},
  {value: 'cost', label: 'Cost'},
  {value: 'handoffs', label: 'Handoffs and reports'},
  {value: 'ideas', label: 'Ideas'},
] as const;

function AgentTree(props: {
  readonly state: OrchestratorState;
  readonly runId: string;
  readonly agentId: string | null;
  readonly pick: (agentId: string) => void;
}): JSX.Element {
  const names = agentNames(props.state, props.runId);
  return (
    <Card section="Agents" flush>
      <Tree
        label="Agents in this run"
        {...(props.agentId === null ? {} : {selected: props.agentId})}
      >
        {flattenTree(agentTree(props.state, props.runId)).map(({agent, depth}) => {
          const word = agentWord(agent.status);
          return (
            <TreeRow
              key={agent.id}
              id={agent.id}
              depth={depth}
              title={names.get(agent.id) ?? 'An agent'}
              subtitle={harnessWord(agent.harness)}
              state={{tone: word.tone, label: word.label}}
              select={() => props.pick(agent.id)}
            />
          );
        })}
      </Tree>
    </Card>
  );
}

/** One run: its agents beside either the chosen agent's turns or the run-wide views. */
export function RunScreen(props: {
  readonly state: OrchestratorState;
  /** What the host is called on screen. */
  readonly host: string;
  readonly runId: string;
  readonly nowMs: number;
  readonly view: RunView;
  readonly agentId: string | null;
  readonly plans: readonly Plan[];
  readonly ideas: IdeasLoad;
  readonly agentPanel: ReactNode;
  readonly goRuns: () => void;
  readonly show: (view: RunView) => void;
  readonly pick: (agentId: string) => void;
}): JSX.Element {
  const run = props.state.runs[props.runId];
  if (run === undefined) {
    return <Text tone="muted">This run is not on this host any more.</Text>;
  }
  const status = runWord(run.status);
  const body =
    props.view === 'timeline' ? (
      <TimelineView
        state={props.state}
        runId={props.runId}
        nowMs={props.nowMs}
        agentId={props.agentId}
        pick={props.pick}
      />
    ) : props.view === 'cost' ? (
      <CostView state={props.state} runId={props.runId} />
    ) : props.view === 'handoffs' ? (
      <HandoffCards state={props.state} runId={props.runId} plans={props.plans} />
    ) : (
      <IdeasView load={props.ideas} />
    );
  return (
    <Stack gap="space-4">
      <Row gap="space-3" wrap>
        <Breadcrumb
          label="Where you are"
          items={[
            {id: 'runs', label: 'Runs'},
            {id: props.runId, label: run.task},
          ]}
        />
        <Chip label={status.label} tone={status.tone} dot />
        <Chip label={props.host} mono />
      </Row>
      <Split
        asideFirst
        aside={
          <AgentTree
            state={props.state}
            runId={props.runId}
            agentId={props.agentId}
            pick={props.pick}
          />
        }
        main={
          props.agentPanel ?? (
            <Tabs label="Views of this run" tabs={VIEWS} value={props.view} change={props.show}>
              {body}
            </Tabs>
          )
        }
      />
    </Stack>
  );
}
