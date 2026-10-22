/**
 * Who ran a turn, from the run's usage records: the harness (the provider CLI) and the model of
 * the execution a turn belongs to, read for finished turns too (the Agents face's live executions
 * name a model only while they run). A turn is named by its span; a record names its span when the
 * journal does.
 */

import type {Span} from '@vibesys/contracts';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import type {AgentNode} from '@vibesys/run-state/agents';
import {agentTurns} from './agent-turns.js';
import {type ModelNames, modelLabel, NO_MODEL_NAMES} from './model-names.js';

/** What ran a turn; a part the journal does not name is `null`. */
export interface Who {
  readonly harness: string | null;
  readonly model: string | null;
  readonly client: 'scripted' | 'provider' | null;
}

/** The harnesses and models by turn span. */
export type WhoBySpan = ReadonlyMap<string, Who>;

/**
 * Who ran each turn the records name by span, each model by the name the host's catalogue gives it
 * (its id when the catalogue lacks it), so every face that words who ran a turn says the same.
 */
export function whoBySpan(
  records: readonly UsageRecord[],
  names: ModelNames = NO_MODEL_NAMES,
): WhoBySpan {
  const by = new Map<string, Who>();
  for (const record of records) {
    if (record.span_id === null) continue;
    const client =
      record.client === 'scripted' || record.client === 'provider' ? record.client : null;
    if (client !== 'scripted' && record.harness === null && record.model === null) continue;
    by.set(record.span_id, {
      harness: client === 'scripted' ? null : record.harness,
      model:
        client === 'scripted' || record.model === null ? null : modelLabel(names, record.model),
      client,
    });
  }
  return by;
}

/** `<model> on <harness>`, or the one part named; null when neither is. */
export function whoWords(who: Who | undefined): string | null {
  if (who === undefined) return null;
  if (who.client === 'scripted') return 'scripted';
  if (who.model !== null && who.harness !== null) return `${who.model} on ${who.harness}`;
  return who.model ?? who.harness;
}

/** The model, else the harness: what a narrow row says of who ran it. */
export function whoShort(who: Who): string | null {
  if (who.client === 'scripted') return 'scripted';
  return who.model ?? who.harness;
}

/** The distinct harness and model pairs of the turns `spans` lists for `agent`, as they ran. */
export function agentWho(
  agent: AgentNode,
  spans: ReadonlyMap<string, Span>,
  who: WhoBySpan,
): readonly Who[] {
  const seen = new Map<string, Who>();
  for (const turn of agentTurns(spans, agent.id)) {
    const ran = who.get(turn.span);
    if (ran !== undefined) seen.set(`${ran.client}\n${ran.harness}\n${ran.model}`, ran);
  }
  return [...seen.values()];
}
