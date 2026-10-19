/**
 * What the Usage page shows, folded from what each project's helper answered for the window: four
 * tiles, the cost of each run and of each kind of agent. The helper sums the turns' records, so
 * nothing here derives a cost from a turn; it only adds the answers of several projects. Every row
 * is a sum of the answers' rows, so the rows add up to the tile that says what they spent, and a
 * count the answers could not place (unpriced turns, agents past the helper's bound, runs it holds
 * no journal of) is carried as a status, never dropped.
 */
import {roleName} from '@vibesys/contracts';
import type {HostKey, UsageWindowEntry} from '@vibesys/hosts';
import {addTokens, NO_TOKENS, type TokenTally, totalTokens} from '@vibesys/run-state';
import {compactTokens} from '@vibesys/run-state/usage';

/** What a project's helper answered for the window, and where the project is. */
export interface UsageSource {
  readonly host: HostKey;
  readonly project: string;
  readonly usage: UsageWindowEntry;
}

/** How an agent's harness and model are named, as the machine's catalogue gives them. */
export interface ModelName {
  /** The provider the catalogue lists the model under (a usage row's harness). */
  readonly harness: string;
  readonly model: string;
  readonly harnessName: string;
  /** The model alone ("Opus 5.5"); null when the catalogue does not name it. */
  readonly modelName: string | null;
}

/** Everything the page folds: the answers and what could not be asked. */
export interface UsageLoaded {
  readonly sources: readonly UsageSource[];
  readonly names: readonly ModelName[];
  /** Projects whose runs or spend could not be read. */
  readonly unreachable: number;
  /** Projects whose machine's VibeSys does not offer usage. */
  readonly unsupported: number;
  /** Runs beyond the helper's bound on one question, which no answer counts. */
  readonly runsLeftOut: number;
}

export interface UsageTile {
  readonly name: string;
  readonly figure: string;
  readonly detail: string | null;
}

export interface RunSpendRow {
  readonly key: string;
  readonly host: HostKey;
  readonly project: string;
  readonly runId: string;
  /** The task the run was started for, else its id: only what equal costs are ordered by. */
  readonly name: string;
  readonly costUsd: number;
  readonly cost: string;
  /** The row's cost against the costliest row's, from 0 to 1. */
  readonly share: number;
}

export interface AgentSpendRow {
  readonly key: string;
  readonly role: string;
  readonly harness: string;
  /** The model as the catalogue names it; its id, when it names it differently, for the pointer. */
  readonly model: {readonly name: string; readonly id: string | null};
  readonly costUsd: number;
  readonly cost: string;
  readonly turns: number;
  readonly share: number;
}

export interface UsageStatus {
  readonly key: string;
  readonly label: string;
}

export interface UsageView {
  /** No agent turn ended in the window. */
  readonly empty: boolean;
  readonly tiles: readonly UsageTile[];
  /** The costliest run first. */
  readonly runs: readonly RunSpendRow[];
  /** The costliest agent first. */
  readonly agents: readonly AgentSpendRow[];
  /** Agents that spent in the window beyond the rows. */
  readonly agentsOmitted: number;
  readonly status: readonly UsageStatus[];
}

const UNKNOWN = 'unknown';
const NONE = 'none';

/** Dollars as the Usage face writes them. */
export function dollars(amount: number): string {
  return `$${amount.toFixed(2)}`;
}

function sum(values: readonly number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

function turnsWords(count: number): string {
  return `${count} ${count === 1 ? 'turn' : 'turns'}`;
}

function tokensDetail(tally: TokenTally): string | null {
  if (tally.input === null && tally.output === null) return null;
  const part = (count: number | null): string => (count === null ? UNKNOWN : compactTokens(count));
  return `in ${part(tally.input)} · out ${part(tally.output)}`;
}

/** The helper's tally in the run fold's terms, so the fold's sum and total serve both. */
function tallyOf(tally: UsageWindowEntry['tokens']): TokenTally {
  return {
    input: tally.input,
    cacheRead: tally.cache_read,
    cacheWrite: tally.cache_write,
    output: tally.output,
    reasoning: tally.reasoning,
  };
}

function tiles(loaded: UsageLoaded): readonly UsageTile[] {
  const answers = loaded.sources.map(source => source.usage);
  const spent = sum(answers.map(answer => answer.spentUsd));
  const turns = sum(answers.map(answer => answer.turns));
  const bests = sum(answers.map(answer => answer.newBests));
  const tally = answers.reduce<TokenTally>(
    (all, answer) => addTokens(all, tallyOf(answer.tokens)),
    NO_TOKENS,
  );
  const total = totalTokens(tally);
  return [
    {
      name: 'Spent',
      figure: dollars(spent),
      detail: turnsWords(turns),
    },
    {
      name: 'Per hour now',
      figure: `${dollars(sum(answers.map(answer => answer.spendPerHourUsd)))}/h`,
      detail: null,
    },
    {
      name: 'Tokens',
      figure: total === null ? UNKNOWN : compactTokens(total),
      detail: tokensDetail(tally),
    },
    {
      name: 'Per new best',
      figure: bests === 0 ? NONE : dollars(spent / bests),
      detail: bests === 0 ? null : `${bests} new ${bests === 1 ? 'best' : 'bests'}`,
    },
  ];
}

function compare(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

/** Costliest first; ties by name, then by key, so the order is total. */
function byCost(
  left: {readonly costUsd: number; readonly name: string; readonly key: string},
  right: {readonly costUsd: number; readonly name: string; readonly key: string},
): number {
  return (
    right.costUsd - left.costUsd || compare(left.name, right.name) || compare(left.key, right.key)
  );
}

function shareOf(cost: number, top: number): number {
  return top > 0 ? cost / top : 0;
}

function runRows(sources: readonly UsageSource[]): readonly RunSpendRow[] {
  const rows = sources.flatMap(({host, project, usage}) =>
    usage.byRun.map(run => ({
      key: JSON.stringify([host, project, run.runId]),
      host,
      project,
      runId: run.runId,
      name: run.task ?? run.runId,
      costUsd: run.costUsd,
    })),
  );
  rows.sort(byCost);
  const top = rows[0]?.costUsd ?? 0;
  return rows.map(row => ({
    ...row,
    cost: dollars(row.costUsd),
    share: shareOf(row.costUsd, top),
  }));
}

interface AgentSum {
  readonly role: string | null;
  readonly harness: string | null;
  readonly model: string | null;
  costUsd: number;
  turns: number;
}

function agentRows(loaded: UsageLoaded): readonly AgentSpendRow[] {
  const sums = new Map<string, AgentSum>();
  for (const {usage} of loaded.sources) {
    for (const agent of usage.byAgent) {
      const key = JSON.stringify([agent.role, agent.harness, agent.model]);
      const found = sums.get(key);
      if (found === undefined) sums.set(key, {...agent});
      else {
        found.costUsd += agent.costUsd;
        found.turns += agent.turns;
      }
    }
  }
  const harnessNames = new Map(loaded.names.map(each => [each.harness, each.harnessName]));
  const modelNames = new Map(
    loaded.names.map(each => [JSON.stringify([each.harness, each.model]), each.modelName]),
  );
  const rows = [...sums.entries()].map(([key, agent]) => {
    const named =
      agent.model === null
        ? null
        : (modelNames.get(JSON.stringify([agent.harness, agent.model])) ?? null);
    return {
      key,
      role: agent.role === null ? UNKNOWN : roleName(agent.role),
      harness:
        agent.harness === null ? UNKNOWN : (harnessNames.get(agent.harness) ?? agent.harness),
      model:
        agent.model === null
          ? {name: UNKNOWN, id: null}
          : {
              name: named ?? agent.model,
              id: named === null || named === agent.model ? null : agent.model,
            },
      costUsd: agent.costUsd,
      turns: agent.turns,
    };
  });
  rows.sort((left, right) =>
    byCost(
      {...left, name: `${left.role} ${left.harness} ${left.model.name}`},
      {...right, name: `${right.role} ${right.harness} ${right.model.name}`},
    ),
  );
  const top = rows[0]?.costUsd ?? 0;
  return rows.map(row => ({...row, cost: dollars(row.costUsd), share: shareOf(row.costUsd, top)}));
}

function status(loaded: UsageLoaded): readonly UsageStatus[] {
  const answers = loaded.sources.map(source => source.usage);
  const not = sum(answers.map(answer => answer.missingRuns.length)) + loaded.runsLeftOut;
  const counted: readonly (readonly [string, number, string])[] = [
    ['unpriced', sum(answers.map(answer => answer.unpricedTurns)), 'unpriced'],
    ['untimed', sum(answers.map(answer => answer.untimedTurns)), 'untimed'],
    ['not-counted', not, 'runs not counted'],
    ['unreachable', loaded.unreachable, 'unreachable'],
    ['unsupported', loaded.unsupported, 'unsupported'],
  ];
  return counted
    .filter(([, count]) => count > 0)
    .map(([key, count, words]) => ({
      key,
      label: `${count} ${key === 'not-counted' && count === 1 ? 'run not counted' : words}`,
    }));
}

/** The page's figures for what the projects answered. */
export function usageView(loaded: UsageLoaded): UsageView {
  const answers = loaded.sources.map(source => source.usage);
  return {
    empty: sum(answers.map(answer => answer.turns)) === 0,
    tiles: tiles(loaded),
    runs: runRows(loaded.sources),
    agents: agentRows(loaded),
    agentsOmitted: sum(answers.map(answer => answer.agentsOmitted)),
    status: status(loaded),
  };
}
