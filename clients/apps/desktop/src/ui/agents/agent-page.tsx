import type {Agent} from '@vibesys/orchestrator-proto';
import {Breadcrumb, Card, Chip, type Crumb, Page, Row, Stack, Text} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  agentNames,
  agentPlace,
  type CommandProgress,
  messageIs,
  messagesOf,
  ms,
  type OrchestratorState,
} from '../../state/index.js';
import {agentWord, harnessWord} from '../orchestrator/words.js';
import {AgentTranscript, type HistoryAccess} from './agent-transcript.js';
import {agentLabel} from './labels.js';
import {MessageBox} from './message-box.js';
import {formatDayTime, formatSpan, formatUsd} from './time.js';

/**
 * Where the agent ran and for how long: the machine it is on, from when to when, and what it took.
 * An agent runs on one machine, so the card has one place; a run that moves agents between machines
 * would draw one for each.
 */
function Placement(props: {
  readonly agent: Agent;
  readonly host: string;
  readonly nowMs: number;
  readonly turns: number;
}): JSX.Element {
  const {agent} = props;
  const from = ms(agent.startedAt) ?? props.nowMs;
  const to = ms(agent.endedAt) ?? props.nowMs;
  const cost = formatUsd(agent.usage?.costUsd ?? null);
  return (
    <Card section="Placement">
      <Row gap="space-4" rowGap="space-2" wrap align="baseline">
        <Chip label={props.host} mono clip />
        <Text variant="small" title="From and to">
          {`${formatDayTime(from)} to ${formatDayTime(to)}`}
        </Text>
        <Text variant="small" tone="muted">
          {[formatSpan(to - from), ...(cost === '' ? [] : [cost]), `${props.turns} turns`].join(
            ' · ',
          )}
        </Text>
      </Row>
    </Card>
  );
}

/**
 * One agent as a page of the run: where it is in the spawn tree and where it ran, then its session
 * read as a conversation in a column of text centred under the page's title, with the box to write
 * to it below. The conversation scrolls inside its own pane, newest at the bottom.
 */
export function AgentPage(props: {
  readonly state: OrchestratorState;
  readonly nowMs: number;
  readonly agent: Agent;
  /** What the machine the run is on is called. */
  readonly host: string;
  /** Where the reader is: the levels above this screen. */
  readonly trail: readonly Crumb[];
  /** The state of every connection that is not working, above the transcript. */
  readonly notice: ReactNode;
  /** What the last press of Send to this agent came to, or undefined before any. */
  readonly sendProgress: CommandProgress | undefined;
  readonly history: HistoryAccess;
  readonly send: (text: string) => void;
  readonly retrySend: () => void;
  /** Opens another agent of the run. */
  readonly open: (agentId: string) => void;
}): JSX.Element {
  const {state, agent} = props;
  const name = agentNames(state, agent.runId).get(agent.id);
  const label = name === undefined ? 'An agent' : agentLabel(name);
  const status = agentWord(agent.status);
  const turns = Object.values(state.turns).filter(turn => turn.agentId === agent.id).length;
  // The lead is at the top of the tree: a place of one level says what the title says.
  const place = agentPlace(state, agent.id);
  return (
    <Page
      key={`agent-${agent.id}`}
      width="wide"
      state="ready"
      trail={props.trail}
      title={label}
      meta={
        <>
          <Chip label={status.label} tone={status.tone} dot />
          <Chip label={props.host} mono clip />
          <Chip label={harnessWord(agent.harness)} />
          <Chip label={agent.model} mono clip />
        </>
      }
    >
      {props.notice}
      <Stack gap="space-5" width="reading">
        {place.length < 2 ? null : (
          <Breadcrumb
            label="Place in the spawn tree"
            items={place.map(crumb => ({
              id: crumb.agentId,
              label: agentLabel(crumb.name),
              ...(crumb.agentId === agent.id ? {} : {press: () => props.open(crumb.agentId)}),
            }))}
          />
        )}
        <Placement agent={agent} host={props.host} nowMs={props.nowMs} turns={turns} />
        <AgentTranscript
          state={state}
          agentId={agent.id}
          host={props.host}
          nowMs={props.nowMs}
          history={props.history}
          open={props.open}
        />
        <MessageBox
          nowMs={props.nowMs}
          agent={agent}
          progress={props.sendProgress}
          sent={messagesOf(state, agent.id).filter(
            message => message.fromAgentId === '' && messageIs(message, 'text'),
          )}
          send={props.send}
          retry={props.retrySend}
        />
      </Stack>
    </Page>
  );
}
