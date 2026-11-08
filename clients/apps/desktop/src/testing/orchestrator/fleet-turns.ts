/**
 * The finished turns of one run of a fleet, so that what the run cost is the sum of what its turns
 * cost: the run's own cost is split across them to the cent and never stated twice. The turns are a
 * function of the run, its agents and the seed, drawn from a stream of their own.
 *
 * - A run with no cost has only unpriced turns: its tokens are known and no price is.
 * - The lead runs on the host's default model, which has no price: its turns are unpriced, unless it is
 *   the run's only agent (then nothing else could carry the cost).
 */
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  type Agent,
  AgentRole,
  type Run,
  type Turn,
  TurnStatus,
  TurnTrigger,
} from '@vibesys/orchestrator-proto';
import {ms} from '../../state/orchestrator/time.js';
import {turnOf, usageOf} from './builders.js';
import {Rng} from './log.js';

const MINUTE = 60_000;

/** One turn and when its last fact is logged. */
export interface TimedTurn {
  readonly at: number;
  readonly turn: Turn;
}

/** `cents` split into `parts` whole shares of random weight that add up to it exactly. */
function split(rng: Rng, cents: number, parts: number): number[] {
  if (parts === 0) return [];
  const weights = Array.from({length: parts}, () => 1 + rng.int(9));
  const total = weights.reduce((sum, weight) => sum + weight, 0);
  const shares = weights.map(weight => Math.floor((cents * weight) / total));
  const last = shares.length - 1;
  shares[last] = (shares[last] ?? 0) + cents - shares.reduce((sum, share) => sum + share, 0);
  return shares;
}

interface Slot {
  readonly agent: Agent;
  readonly ordinal: number;
  readonly startMs: number;
  readonly endMs: number;
}

/** Each agent's turns, in order, filling the time from its start to the run's end (or `nowMs`). */
function slotsOf(rng: Rng, run: Run, agents: readonly Agent[], nowMs: number): Slot[] {
  const live = ms(run.endedAt) === null;
  const untilMs = (ms(run.endedAt) ?? nowMs) - MINUTE;
  // A live run is still working: its newest turns end within the last 45 minutes, after all the others.
  const earlyUntilMs = live ? untilMs - 50 * MINUTE : untilMs;
  return agents.flatMap(agent => {
    const fromMs = (ms(agent.startedAt) ?? 0) + MINUTE;
    const count = 2 + rng.int(3);
    const width = Math.max(MINUTE, Math.floor((earlyUntilMs - fromMs) / count));
    const slots = Array.from({length: count}, (_, index) => ({
      agent,
      ordinal: index + 1,
      startMs: fromMs + index * width,
      endMs: Math.min(earlyUntilMs, fromMs + index * width + Math.floor(width * 0.8)),
    }));
    if (!live) return slots;
    const endMs = untilMs - rng.int(45) * MINUTE;
    return [...slots.slice(0, -1), {agent, ordinal: count, startMs: endMs - MINUTE, endMs}];
  });
}

/** The turns of `run`: every one finished, and the cost of the priced ones adds up to the run's. */
export function fleetTurns(
  seed: number,
  run: Run,
  agents: readonly Agent[],
  nowMs: number,
): TimedTurn[] {
  const rng = new Rng(seed * 7919 + (ms(run.createdAt) ?? 0));
  const slots = slotsOf(rng, run, agents, nowMs);
  const alone = agents.length === 1;
  const cost = run.usage?.costUsd;
  const priced =
    cost === undefined ? [] : slots.filter(slot => alone || slot.agent.role !== AgentRole.LEAD);
  const shares = split(rng, Math.round((cost ?? 0) * 100), priced.length);
  return slots.map(slot => {
    const index = priced.indexOf(slot);
    const turn = {
      ...turnOf({
        id: `${slot.agent.id}-turn-${slot.ordinal}`,
        agentId: slot.agent.id,
        ordinal: slot.ordinal,
        trigger: TurnTrigger.BRIEF,
        triggerMessageId: `${run.id}-brief`,
        startMs: slot.startMs,
        status: TurnStatus.COMPLETED,
      }),
      endedAt: timestampFromMs(slot.endMs),
      usage: usageOf({
        input: rng.between(2_000, 40_000),
        output: rng.between(300, 6_000),
        cacheRead: rng.between(10_000, 200_000),
        ...(index < 0 ? {} : {cost: (shares[index] ?? 0) / 100}),
      }),
    };
    return {at: slot.endMs, turn};
  });
}
