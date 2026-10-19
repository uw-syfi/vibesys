/**
 * The Agents face: a tree of who started whom (`model.agents`, from `@vibesys/run-state/agents`),
 * each agent drawn once under its first starter with `+ also under <role>` for fan-in, finished
 * subtrees folded (`agentTree`); and the pane of one opened agent: its facts and its turns.
 */
import type {AgentNode} from '@vibesys/run-state/agents';
import {
  Button,
  EmptyState,
  KeyValue,
  PickList,
  PickRow,
  type RowBadge,
  Stack,
  Text,
} from '@vibesys/ui';
import {type JSX, useMemo} from 'react';
import {type AgentRow, agentTree, type FoldRow} from '../../../../state/agent-tree.js';
import {type AgentTurn, agentModel, agentTurns} from '../../../../state/agent-turns.js';
import {
  agentWho,
  roleLabel,
  spendOf,
  type TokensCells,
  tokensByAgent,
  tokensCells,
  tokensColumn,
  tokensLine,
  tokensTitle,
  turnTokens,
} from '../../../../state/index.js';
import {type QuotaPauseSpan, workedWords} from '../../../../state/quota-pauses.js';
import {roleSeries} from '../../../../state/role-series.js';
import {statusWords} from '../../../../state/span-words.js';
import type {ViewBase, WithCore} from '../props.js';
import {whoBadge} from '../who-badge.js';

export interface AgentsViewProps extends ViewBase {
  /** The opened agent's id, highlighted in the list; null at the run level. */
  readonly selected: string | null;
  /** Beside an opened agent: no header or activity column. */
  readonly compact: boolean;
  /**
   * The agents whose finished agents show rather than fold, by id. The page keeps them, so a
   * group the user unfolded stays unfolded in the list beside an agent opened from anywhere.
   */
  readonly unfolded: ReadonlySet<string>;
  /** Unfolds the finished agents under `parent`. */
  readonly unfold: (parent: string) => void;
}

type Status = AgentNode['status'];

function tone(status: Status): 'live' | 'ok' | 'err' | 'warn' | 'neutral' {
  switch (status) {
    case 'running':
      return 'live';
    case 'done':
      return 'ok';
    case 'failed':
      return 'err';
    case 'waiting':
      return 'warn';
    default:
      return 'neutral';
  }
}

const ENDED: ReadonlySet<Status> = new Set<Status>(['done', 'failed', 'stopped']);

/** How long an agent has been at work: its first turn's start to its latest turn's end, or now. */
function took(
  agent: AgentNode,
  now: number | null,
  pauses: readonly QuotaPauseSpan[],
): string | undefined {
  const end = ENDED.has(agent.status) ? agent.activity.endedAt : now;
  return end === null ? undefined : workedWords(pauses, agent.startedAt, end);
}

function AgentLine(props: {
  readonly row: AgentRow;
  readonly view: AgentsViewProps;
  /** The agent's cells of the in and out columns; null when the list has no such columns. */
  readonly measure: TokensCells | null;
}): JSX.Element {
  const {row, view} = props;
  const {agent} = row;
  const summary = [agent.activity.within?.title, row.headline]
    .filter((part): part is string => part !== undefined && part !== null)
    .join(' · ');
  const [first, ...rest] = agentWho(agent, view.model.trace.spans, view.who);
  const ran = whoBadge(first, rest.length);
  const also = [
    ...(ran === null ? [] : [ran]),
    ...(row.alsoUnder.length === 0 ? [] : [alsoBadge(row.alsoUnder, view.compact)]),
  ];
  const time = took(agent, view.clock.now, view.model.pauses);
  const {measure} = props;
  return (
    <PickRow
      id={agent.id}
      title={roleLabel(agent.role)}
      swatch={roleSeries(agent.role)}
      {...(view.compact || summary === '' ? {} : {summary})}
      guides={row.guides}
      state={{tone: tone(agent.status), label: statusWords(agent.status).label}}
      badges={also}
      figure={agent.turns.toLocaleString('en-US')}
      figureTone="muted"
      {...(measure === null
        ? {}
        : {
            measure: [
              {...measure.input, size: 'note' as const},
              {...measure.output, size: 'plain' as const},
            ],
          })}
      {...(time === undefined || view.compact ? {} : {trail: {label: time, short: true}})}
      select={() => view.zoom({type: 'open-agent', agent: agent.id})}
    />
  );
}

/** The fan-in chip: the roles that also started the agent, or only how many in a narrow list. */
function alsoBadge(roles: readonly string[], compact: boolean): RowBadge {
  const count = `+${roles.length} parent`;
  const whole = `also started by ${roles.join(', ')}`;
  return compact ? {label: count, tone: 'neutral'} : {label: whole, short: count, tone: 'neutral'};
}

function FoldLine(props: {readonly row: FoldRow; readonly show: () => void}): JSX.Element {
  return (
    <PickRow
      id={`fold:${props.row.parent}`}
      title={`+ ${props.row.count} finished`}
      summary="show them"
      guides={props.row.guides}
      select={props.show}
    />
  );
}

export function AgentsView(props: AgentsViewProps): JSX.Element {
  const {agents} = props.model;
  const spans = props.model.trace.spans;
  const {unfolded, unfold} = props;
  const spent = useMemo(() => tokensByAgent(spans, props.tokens), [spans, props.tokens]);
  const rows = useMemo(
    () => agentTree(agents, spans, unfolded, props.selected),
    [agents, spans, unfolded, props.selected],
  );
  // The tokens column is for the full list: beside an opened agent there is no room for it.
  const agentRows = rows.filter(row => row.kind === 'agent');
  const cells = tokensCells(
    props.compact
      ? agentRows.map(() => undefined)
      : agentRows.map(row => spendOf(spent.get(row.agent.id), row.agent.status)),
  );
  const cellOf = new Map(agentRows.map((row, index) => [row.agent.id, cells[index] ?? null]));
  const hasTokens = cells.some(cell => cell !== null);
  if (agents.agents.length === 0) return <EmptyState title="No agents yet" />;
  return (
    <PickList
      label="Agents"
      size="fill"
      {...(props.selected === null ? {} : {selected: props.selected})}
      {...(props.compact
        ? {}
        : {
            header: {
              start: 'agent · what it is doing',
              end: [
                {label: 'turns', fixed: 'figure' as const},
                ...(hasTokens
                  ? [
                      {label: 'in (cached)', fixed: 'measure-note' as const},
                      {label: 'out', fixed: 'measure' as const},
                    ]
                  : []),
                {label: 'took', fixed: 'time' as const},
              ],
            },
          })}
    >
      {rows.map(row =>
        row.kind === 'agent' ? (
          <AgentLine
            key={row.agent.id}
            row={row}
            view={props}
            measure={cellOf.get(row.agent.id) ?? null}
          />
        ) : (
          <FoldLine key={`fold:${row.parent}`} row={row} show={() => unfold(row.parent)} />
        ),
      )}
    </PickList>
  );
}

export interface AgentPaneProps extends ViewBase, WithCore {
  readonly agent: AgentNode;
}

/** The body of an opened agent's pane (the frame draws the pane): its facts, then its turns. */
export function AgentPane(props: AgentPaneProps): JSX.Element {
  const {agent, model, zoom} = props;
  const spans = model.trace.spans;
  const turns = useMemo(() => agentTurns(spans, agent.id), [spans, agent.id]);
  const ran = agentWho(agent, spans, props.who);
  const models = [...new Set(ran.flatMap(each => each.model ?? []))];
  const harnesses = [...new Set(ran.flatMap(each => each.harness ?? []))];
  const modelName = models.length > 0 ? models.join(', ') : agentModel(agent, props.core);
  const spent = tokensByAgent(spans, props.tokens).get(agent.id);
  const spentLine = spent === undefined ? null : tokensLine(spent);
  const parents = agent.startedBy
    .map(id => model.agents.agents.find(item => item.id === id))
    .filter((item): item is AgentNode => item !== undefined);
  return (
    <Stack gap="space-3">
      <KeyValue
        items={[
          {key: 'Role', value: roleLabel(agent.role)},
          {key: 'Model', value: modelName ?? 'The run’s default', mono: modelName !== null},
          ...(harnesses.length === 0
            ? []
            : [{key: 'Harness', value: harnesses.join(', '), mono: true}]),
          {
            key: 'Started by',
            value:
              parents.length === 0
                ? 'The search'
                : parents.map(parent => (
                    <Button
                      key={parent.id}
                      size="sm"
                      variant="quiet"
                      text={roleLabel(parent.role)}
                      press={() => zoom({type: 'open-agent', agent: parent.id})}
                    />
                  )),
          },
          {key: 'Turns', value: String(agent.turns)},
          ...(spent === undefined || spentLine === null
            ? []
            : [
                {
                  key: 'Tokens',
                  value: <span title={tokensTitle(spent)}>{spentLine}</span>,
                },
              ]),
          {key: 'Status', value: statusWords(agent.status).label},
        ]}
      />
      <TurnList
        turns={turns}
        clock={props.clock}
        pauses={model.pauses}
        tokens={props.tokens}
        zoom={zoom}
      />
    </Stack>
  );
}

function TurnList(props: {
  readonly turns: readonly AgentTurn[];
  readonly clock: ViewBase['clock'];
  readonly pauses: readonly QuotaPauseSpan[];
  readonly tokens: ViewBase['tokens'];
  readonly zoom: ViewBase['zoom'];
}): JSX.Element {
  if (props.turns.length === 0) {
    return (
      <Text variant="small" tone="muted">
        No turns yet
      </Text>
    );
  }
  const cells = tokensColumn(
    props.turns.map(turn => spendOf(turnTokens(props.tokens, turn.span), turn.status)),
  );
  return (
    <PickList label="Turns" size="fill">
      {props.turns.map((turn, index) => {
        const measure = cells[index] ?? null;
        const end = turn.endedAt ?? props.clock.now;
        return (
          <PickRow
            key={turn.span}
            id={turn.span}
            lead={String(turn.number)}
            title={turn.headline ?? `Turn ${turn.number}`}
            state={{tone: tone(turn.status), label: statusWords(turn.status).label}}
            {...(end === null ? {} : {figure: workedWords(props.pauses, turn.startedAt, end)})}
            figureTone="muted"
            {...(measure === null ? {} : {measure: {...measure, size: 'line' as const}})}
            select={() => props.zoom({type: 'open-step', step: turn.span})}
          />
        );
      })}
    </PickList>
  );
}
