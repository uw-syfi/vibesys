/**
 * The Agents face: a tree of who started whom (`model.agents`, from `@vibesys/run-state/agents`),
 * each agent drawn once under its first starter with `+ also under <role>` for fan-in, finished
 * subtrees folded; and the pane of one opened agent (its latest turn as narrative).
 *
 * Stub: a flat list of the agents that opens each one; the tree is still to build (#2997).
 */
import type {AgentNode} from '@vibesys/run-state/agents';
import {EmptyState, PickList, PickRow, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {ViewBase, WithCore} from '../props.js';

export interface AgentsViewProps extends ViewBase {
  /** The opened agent's id, highlighted in the list; null at the run level. */
  readonly selected: string | null;
  /** Beside an opened agent: no header or activity column. */
  readonly compact: boolean;
}

export function AgentsView(props: AgentsViewProps): JSX.Element {
  const {agents} = props.model.agents;
  if (agents.length === 0) return <EmptyState title="No agents yet" />;
  return (
    <PickList
      label="Agents"
      size="fill"
      {...(props.selected === null ? {} : {selected: props.selected})}
    >
      {agents.map(agent => (
        <PickRow
          key={agent.id}
          id={agent.id}
          title={agent.role}
          {...(props.compact ? {} : {summary: agent.activity.within?.title ?? 'agent turn'})}
          figure={`${agent.turns}`}
          figureTone="muted"
          state={{
            tone: agent.status === 'running' ? 'live' : 'neutral',
            label: agent.status,
          }}
          select={() => props.zoom({type: 'open-agent', agent: agent.id})}
        />
      ))}
    </PickList>
  );
}

export interface AgentPaneProps extends ViewBase, WithCore {
  readonly agent: AgentNode;
}

/** The body of an opened agent's pane (the frame draws the pane): its latest turn. */
export function AgentPane(props: AgentPaneProps): JSX.Element {
  return (
    <Text variant="small" tone="muted">
      {`${props.agent.turns} turns · latest ${props.agent.activity.status}`}
    </Text>
  );
}
