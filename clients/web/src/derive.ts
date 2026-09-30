/** Pure helpers shared by the view modules: formatting, prose, steers, backfill, run control. */
import type {ProtocolResponse, RunEvent} from '@vibesys/backend-client';
import {
  type AgentPhase,
  type CoreState,
  hasRunEnded,
  roundNumberFromLabel,
  type TranscriptEntry,
} from '@vibesys/core-state';
import type {
  Connection,
  ConsumedSteer,
  EndedWord,
  PendingSteer,
  ProsePart,
  RunControl,
  Steers,
} from './model.js';

const compact = new Intl.NumberFormat('en', {notation: 'compact', maximumSignificantDigits: 4});
const grouped = new Intl.NumberFormat('en', {maximumSignificantDigits: 5});

/** `1,045` below 100k, where compact notation would hide digits a reader compares; `112.7M` above. */
export function formatValue(value: number): string {
  return Math.abs(value) < 100_000 ? grouped.format(value) : compact.format(value);
}

/** The latest round the run has started, or null before round 1. */
export function latestRound(core: CoreState): number | null {
  return core.rounds.filter(round => round.status !== 'planned').at(-1)?.number ?? null;
}

/** The newest started agent's scope, including run-scoped calls between recorded rounds. */
export function activityRound(core: CoreState): number | null {
  const phase = latestStartedPhase(core.phases.filter(candidate => candidate.status !== 'pending'));
  return phase === undefined ? latestRound(core) : phase.roundNumber;
}

/** Selects by observed start, since pending role slots are replaced in place. */
export function latestStartedPhase(phases: readonly AgentPhase[]): AgentPhase | undefined {
  return [...phases].sort((left, right) => phaseStart(left) - phaseStart(right)).at(-1);
}

/** Missing starts precede observed starts; stable sorting preserves ties. */
function phaseStart(phase: AgentPhase): number {
  const time = Date.parse(phase.startedAt ?? '');
  return Number.isNaN(time) ? Number.NEGATIVE_INFINITY : time;
}

/** `perf_eval` as `Perf eval`: how a role or a harness is written as a word. */
export function titleCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1).replaceAll('_', ' ');
}

/** Where a consumed control event delivered its steers: the consuming call's scope. */
function consumer(event: RunEvent): Omit<ConsumedSteer, 'id' | 'text'> {
  return {
    sequence: event.sequence ?? 0,
    round: roundNumberFromLabel(event.round_label),
    roundLabel: event.round_label ?? null,
    agentKind: event.agent_kind ?? null,
    executionId: event.execution_id ?? event.invocation_id ?? null,
  };
}

export function steers(captured: readonly RunEvent[]): Steers {
  let pending: PendingSteer[] = [];
  const consumed: ConsumedSteer[] = [];
  for (const event of captured) {
    if (event.type !== 'control' || !event.text?.startsWith('/steer')) continue;
    if (event.status === 'pending' && event.text.startsWith('/steer: ')) {
      pending.push({id: `steer-${event.sequence}`, text: event.text.slice('/steer: '.length)});
    } else if (event.status === 'consumed') {
      // One consumed event delivers every steer queued since the previous one.
      const scope = consumer(event);
      for (const steer of pending) consumed.push({...steer, ...scope});
      pending = [];
    }
  }
  return {pending, consumed};
}

const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/;

/** Paragraphs of inline code, bold, and plain text. Nothing else of Markdown is rendered. */
export function prose(text: string): ProsePart[][] {
  return text
    .trim()
    .split(/\n{2,}/)
    .map(paragraph =>
      paragraph
        .split(INLINE)
        .filter(token => token !== '')
        .map((token): ProsePart => {
          if (token.length > 2 && token.startsWith('`') && token.endsWith('`')) {
            return {kind: 'code', text: token.slice(1, -1)};
          }
          if (token.length > 4 && token.startsWith('**') && token.endsWith('**')) {
            return {kind: 'strong', text: token.slice(2, -2)};
          }
          return {kind: 'text', text: token};
        }),
    );
}

/** Strips the run workspace prefix (`/…/<run id>/`) from paths at the start of a word. */
export function pathShortener(runId: string | null): (text: string) => string {
  if (runId === null) return text => text;
  const escaped = runId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const prefix = new RegExp(String.raw`(?<=^|[\s'"=(])/[^\s'"]*?/${escaped}/`, 'g');
  return text => text.replace(prefix, '');
}

/**
 * How long the call took. The command payload is the only one that reports it, whatever tool
 * produced that payload, so most timed rows are Bash and a few are not. One decimal under a
 * minute, so a right-aligned column of times lines up (`7.9s`, `12.2s`), then `1m 05s`. `<0.1s`
 * for the 40 ms reads that are most of a round: `0.0s` would claim a call took no time at all.
 */
export function toolDuration(entry: TranscriptEntry): string | null {
  const payload = entry.toolResult?.payload;
  const seconds = payload?.kind === 'command' ? payload.duration : null;
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 0.1) return '<0.1s';
  if (seconds < 59.95) return `${seconds.toFixed(1)}s`;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, '0')}s`;
}

/**
 * Whether the round's history may sit below the tail floor and needs `loadOlder()`: true until
 * an earlier round's entries above the floor show where this round starts. Spine entries at or
 * below the floor (a replayed `round_finished`) say nothing about what was skipped.
 */
export function needsOlder(core: CoreState, round: number | null): boolean {
  const floor = core.historyAfterSequence;
  // R0 is the objective's baseline, not a round: it has no events.
  if (floor === 0 || round === 0) return false;
  if (round === null) return true;
  const earliest = core.transcript.find(
    entry => entry.roundNumber !== undefined && Number(entry.id) > floor,
  )?.roundNumber;
  return earliest === undefined || round <= earliest;
}

/**
 * Whether the text of a steer `round` consumed may still sit below the tail floor. The backend
 * drains queued steers when an agent call starts and journals only `/steer` on the consuming
 * call, so what call C consumed was queued after the call before C started: backfill until that
 * start (its `phase_started` entry) is above the floor. `needsOlder` already covers a C-1 inside
 * `round`, and a still-queued steer (sent after the latest call started), so this adds R-1's last
 * call when `round`'s first call consumed a steer.
 */
export function steersNeedOlder(
  core: CoreState,
  captured: readonly RunEvent[],
  round: number | null,
): boolean {
  const floor = core.historyAfterSequence;
  if (floor === 0) return false;
  const consumed = captured.find(
    event =>
      event.type === 'control' &&
      event.status === 'consumed' &&
      event.text?.startsWith('/steer') === true &&
      roundNumberFromLabel(event.round_label) === round,
  );
  const at = consumed?.sequence;
  if (consumed === undefined || at === undefined) return false;
  // ponytail: a steer sent between C-1's drain and its phase_started event sits just below that
  // start; core-state keeps no earlier call boundary, so such a steer can stay unloaded.
  return !core.transcript.some(
    entry =>
      entry.kind === 'status' &&
      entry.roundNumber !== undefined &&
      Number(entry.id) > floor &&
      Number(entry.id) < at &&
      // The consuming call's own start can precede its control event.
      !(entry.roundLabel === consumed.round_label && entry.agentKind === consumed.agent_kind),
  );
}

export function endedWord(core: CoreState, captured: readonly RunEvent[]): EndedWord | null {
  if (!hasRunEnded(core)) return null;
  if (captured.some(event => event.type === 'run_interrupted')) return 'Interrupted';
  return core.status === 'completed' ? 'Completed' : 'Failed';
}

/** The one run control, from `core.status` and the connection only. Resume only in `paused`. */
export function runControl(
  core: CoreState,
  captured: readonly RunEvent[],
  connection: Connection,
): RunControl {
  const word = endedWord(core, captured);
  if (word !== null) {
    const diagnostic = core.diagnostics.filter(candidate => candidate.scope === 'run').at(-1);
    if (word === 'Completed' || diagnostic === undefined) {
      return {kind: 'ended', word, summary: null, tip: null};
    }
    return {
      kind: 'ended',
      word,
      summary: diagnostic.summary,
      tip: diagnostic.detail ?? diagnostic.summary,
    };
  }
  const offline = connection !== 'connected';
  switch (core.status) {
    case 'running':
      return {
        kind: 'action',
        action: 'pause',
        label: 'Pause',
        tip: 'Pause after the current agent call',
        disabled: offline,
      };
    case 'pausing':
      return {
        kind: 'action',
        action: 'pause',
        label: 'Pausing',
        tip: 'Pausing after the current agent call',
        disabled: true,
      };
    case 'paused':
      return {
        kind: 'action',
        action: 'resume',
        label: 'Resume',
        tip: 'Resume the run',
        disabled: offline,
      };
    default:
      return {
        kind: 'action',
        action: 'pause',
        label: 'Pause',
        tip: 'Pause after the current agent call',
        disabled: true,
      };
  }
}

/**
 * The objective as tooltip text, and its first sentence (up to the first `.`, `!`, or `?` before a
 * space or line end). Soft wraps become spaces; paragraph breaks and list-item lines (`- `, `* `,
 * `1. `) stay; inline backticks are dropped.
 */
export function objectiveText(
  text: string | null | undefined,
): {first: string; full: string} | null {
  const full = text
    ?.split(/\n[ \t]*\n/)
    .map(paragraph => paragraph.trim().replace(/[ \t]*\n(?![ \t]*(?:[-*] |\d+\. ))[ \t]*/g, ' '))
    .filter(Boolean)
    .join('\n\n')
    .replace(/`([^`]*)`/g, '$1');
  if (!full) return null;
  const line = full.split('\n')[0] ?? full;
  // ponytail: an abbreviation such as "e.g." ends the sentence early; the tooltip has the rest.
  const first = /^.*?[.!?](?=\s|$)/.exec(line)?.[0] ?? line;
  return {first, full};
}

/**
 * The open run's rounds note while the experiments query says the project has not attached: the
 * run waits for it, or ended without it.
 */
export function attachNote(experiments: ProtocolResponse | null, ended: boolean): string | null {
  if (experiments?.experiments_ready !== false) return null;
  return ended ? 'The run ended before the project attached' : 'Waiting for the project to attach';
}
