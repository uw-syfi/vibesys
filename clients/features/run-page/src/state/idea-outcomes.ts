/**
 * What each idea's implementer said it did: the choice its structured reply made (`outcome`, as a
 * short chip) and the reply's summary, folded per idea from the transcript the step view already
 * reads. The latest implementer turn of the latest attempt that ended in a typed reply speaks for
 * the idea. The reply's own JSON Schema says which field is a choice and which is prose
 * (`replyFields`); the field whose key names an outcome is the outcome, the summary is the one the
 * step view leads with (`replyOverview`), so a reply of any shape reads the same way here.
 *
 * Pure over the candidates and the transcript; memoized on both, so a render with neither changed
 * folds nothing.
 */
import type {CoreState} from '@vibesys/run-state';
import type {Candidate, CandidateTree} from '@vibesys/run-state/candidates';
import {type TranscriptEntry, transcriptBySpan} from '@vibesys/run-state/transcript';
import {replyFields} from './reply-fields.js';
import {words} from './span-reading.js';
import {replyOverview} from './step-narrative.js';
import type {Tone} from './views.js';

/** What an idea's implementer reported; a part it did not report is null. */
export interface ImplementerOutcome {
  /** The chosen outcome, in the words the reply used: `nominated`, `blocked`. */
  readonly outcome: {readonly label: string; readonly tone: Tone} | null;
  readonly summary: string | null;
}

export type ImplementerOutcomes = ReadonlyMap<string, ImplementerOutcome>;

const IMPLEMENTER = 'implementer';

/** The implementer turns' spans of `item`, latest attempt and latest step first. */
function implementerSpans(item: Candidate): string[] {
  return [...item.attempts]
    .reverse()
    .flatMap(attempt => [...attempt.steps].reverse())
    .filter(step => step.role === IMPLEMENTER)
    .map(step => step.span);
}

function outcomeOf(entry: TranscriptEntry): ImplementerOutcome | null {
  const reply = entry.reply;
  if (reply === undefined) return null;
  const fields = replyFields(
    reply.json_schema as Readonly<Record<string, unknown>>,
    reply.value as Readonly<Record<string, unknown>>,
  );
  const choice = fields.find(field => field.kind === 'badge' && /outcome/i.test(field.key));
  const key = replyOverview(fields)[0];
  const prose = fields.find(field => field.key === key);
  const outcome =
    choice?.kind === 'badge'
      ? {label: words(choice.value), tone: choice.tone ?? ('neutral' as const)}
      : null;
  const summary = prose?.kind === 'prose' ? prose.text : null;
  return outcome === null && summary === null ? null : {outcome, summary};
}

function foldOutcomes(
  transcript: readonly TranscriptEntry[],
  tree: CandidateTree,
): ImplementerOutcomes {
  const spans = new Map(tree.candidates.map(item => [item.id, implementerSpans(item)]));
  const wanted = new Set([...spans.values()].flat());
  const entries = transcriptBySpan(transcript, wanted);
  const found = new Map<string, ImplementerOutcome>();
  for (const [id, own] of spans) {
    for (const span of own) {
      const replies = (entries.get(span) ?? []).filter(entry => entry.kind === 'reply');
      const latest = replies.at(-1);
      const outcome = latest === undefined ? null : outcomeOf(latest);
      if (outcome === null) continue;
      found.set(id, outcome);
      break;
    }
  }
  return found;
}

const NONE: ImplementerOutcomes = new Map();
const memo = new WeakMap<readonly TranscriptEntry[], WeakMap<CandidateTree, ImplementerOutcomes>>();

/** The implementer outcome of each idea that has one, by idea id; empty before the run's fold. */
export function implementerOutcomes(
  core: CoreState | null,
  tree: CandidateTree,
): ImplementerOutcomes {
  if (core === null) return NONE;
  const transcript = core.transcript;
  const byTree = memo.get(transcript) ?? new WeakMap<CandidateTree, ImplementerOutcomes>();
  memo.set(transcript, byTree);
  const cached = byTree.get(tree);
  if (cached !== undefined) return cached;
  const folded = foldOutcomes(transcript, tree);
  byTree.set(tree, folded);
  return folded;
}
