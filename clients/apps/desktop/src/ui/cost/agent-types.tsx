/**
 * The agents of a run, or of a host, by type with what each type has cost: one set of columns in one
 * order on every page that shows it (the run report's panel and the Cost page's): the type, how many
 * of its agents are working, its turns, its input and output tokens (principle 19: split, and a part
 * nobody reported is `unknown`), and its cost. Each page builds its lines from its own data with
 * `agentTypeLine`, so how a figure is written is decided here, once.
 */
import {Table, type TableColumn, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CostUsage} from '../../state/index.js';
import {type CostCell, costCell} from './cost-cell.js';
import {type TokenSplit, tokenSplit} from './token-split.js';
import {formatCount, NOT_REPORTED, type Precision} from './words.js';

/** One row of the table, every figure already written. */
export interface AgentTypeLine {
  readonly key: string;
  readonly name: string;
  /** `2 of 5`: how many of the type's agents are working, of how many there have been. */
  readonly running: string;
  readonly turns: string;
  readonly tokens: TokenSplit;
  readonly cost: CostCell;
}

/** What the cost view says of a type: absent while no cost view has been read. */
export interface TypeSpend {
  readonly turns: number;
  readonly unpricedTurns: number;
  readonly usage: CostUsage;
}

/** The line of a type from the host's counts of its agents and the cost view's figures of it. */
export function agentTypeLine(type: {
  readonly key: string;
  readonly name: string;
  readonly running: number;
  readonly total: number;
  readonly spend: TypeSpend | null;
  readonly precision: Precision;
}): AgentTypeLine {
  const {spend} = type;
  return {
    key: type.key,
    name: type.name,
    running: `${formatCount(type.running)} of ${formatCount(type.total)}`,
    turns: spend === null ? NOT_REPORTED : formatCount(spend.turns),
    tokens:
      spend === null
        ? {input: NOT_REPORTED, cached: NOT_REPORTED, output: NOT_REPORTED}
        : tokenSplit(spend.usage, spend.turns),
    cost:
      spend === null
        ? {text: NOT_REPORTED, hint: undefined}
        : costCell(spend.usage, spend.turns, spend.unpricedTurns, type.precision),
  };
}

const INPUT_HINT = 'Input tokens: fresh input, cache reads and cache writes';

const COLUMNS: readonly TableColumn<AgentTypeLine>[] = [
  {key: 'type', header: 'Type', lead: true, clip: line => line.name, cell: line => line.name},
  {
    key: 'running',
    header: 'Running',
    hint: 'Agents working now, of all the type has had',
    mono: true,
    align: 'end',
    cell: line => line.running,
  },
  {key: 'turns', header: 'Turns', mono: true, align: 'end', cell: line => line.turns},
  {
    key: 'input',
    header: 'Input',
    hint: INPUT_HINT,
    mono: true,
    align: 'end',
    cell: line => (
      <Text
        variant="code"
        tone="muted"
        title={`${line.tokens.cached} of the input was read from a cache`}
      >
        {line.tokens.input}
      </Text>
    ),
  },
  {
    key: 'output',
    header: 'Output',
    hint: 'Output tokens',
    mono: true,
    align: 'end',
    cell: line => line.tokens.output,
  },
  {
    key: 'cost',
    header: 'Cost',
    mono: true,
    align: 'end',
    cell: line => (
      <Text variant="code" {...(line.cost.hint === undefined ? {} : {title: line.cost.hint})}>
        {line.cost.text}
      </Text>
    ),
  },
];

/** The agents by type, in the columns every page writes them in; `empty` is what it says with no row. */
export function AgentTypeTable(props: {
  readonly label: string;
  readonly rows: readonly AgentTypeLine[];
  readonly empty: string;
}): JSX.Element {
  return (
    <Table
      label={props.label}
      dense
      middle
      columns={COLUMNS}
      rows={props.rows}
      rowKey={line => line.key}
      empty={props.empty}
    />
  );
}
