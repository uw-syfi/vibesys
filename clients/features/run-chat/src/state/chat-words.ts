/** The words the run chat says, apart from the components that show them. */
import {roleName} from '@vibesys/contracts';
import type {ChatFailure} from './failure.js';
import type {SteerDraft, SteerMode, SteerTargetKind} from './steer.js';

/** The closed disclosure that holds a failure's diagnostic. */
export const DETAILS_LABEL = 'Details';
/** The notice under an answer whose turn failed. */
export const FAILED_ANSWER = 'Failed';
/** The notice under an answer the agent is still writing. */
export const WRITING_ANSWER = 'Writing';
/**
 * Why a new thread has no harness, when the run's own is unknown too: the panel picks from any
 * harness the machine lists, so either the list could not be read or it was empty.
 */
export const HARNESS_LIST_UNREAD = 'The harness list could not be read';
export const NO_HARNESS_OFFERED = 'This machine offers no harness to chat with';

/** What a steer to the whole run does with each When choice. */
const WHOLE_RUN_HELP: Readonly<Record<SteerMode, string>> = {
  next_turn: 'Added once, to the prompt of the first agent to start a turn.',
  standing:
    "Added to every agent's prompt at each later turn, until it is withdrawn or the run ends.",
  interrupt:
    'Sent to one agent mid-turn if one is working and can take it, else added once to the next turn that starts.',
};

/** Whom a steer to one role, attempt or item reaches, by name, and what ends its standing guidance. */
function addressee(
  kind: Exclude<SteerTargetKind, 'run'>,
  id: string,
): {readonly name: string; readonly ends: string} {
  switch (kind) {
    case 'role':
      return {name: `the ${roleName(id)}`, ends: 'the run'};
    case 'attempt':
      return {name: `attempt ${id}`, ends: 'the attempt'};
    case 'item':
      return {name: `item ${id}`, ends: 'the item'};
  }
}

/**
 * What a steer does with its When and To choices, in one sentence under the card's choices
 * (`SteerMode` in the run protocol). Next turn reaches one turn: for the whole run, the first agent
 * to start one. Every turn reaches each later turn of the target until the steer is withdrawn or
 * the target ends. Now is offered to one running turn whose provider takes a message mid-turn,
 * else it waits for the next turn. Withdrawing is said as a fact, not offered as a control: the
 * card's Withdraw lasts only while the run's page is open.
 */
export function steerHelp(draft: Pick<SteerDraft, 'mode' | 'target'>): string {
  const {kind, id} = draft.target;
  if (kind === 'run') return WHOLE_RUN_HELP[draft.mode];
  const who = addressee(kind, id ?? '');
  switch (draft.mode) {
    case 'next_turn':
      return `Added once, to the prompt of ${who.name}'s next turn.`;
    case 'standing':
      return `Added to ${who.name}'s prompt at each later turn, until it is withdrawn or ${who.ends} ends.`;
    case 'interrupt':
      return `Sent to ${who.name} mid-turn if it is working and can take it, else added once to its next turn.`;
  }
}

/** An error as the run chat is asked about it: where it happened, what it said, and its diagnostic. */
export interface ErrorReport {
  readonly where: string;
  readonly message: string;
  readonly detail: string | null;
}

export function failedAnswerReport(text: string): ErrorReport {
  return {where: 'Run chat answer', message: text, detail: null};
}

export function steerFailureReport(failure: ChatFailure): ErrorReport {
  return {where: 'Steer', ...failure};
}

/** The smallest cap a question is cut to: room for the words around the error and a short `where`. */
const MIN_QUESTION_BYTES = 1024;
/** `where` is a short label; a longer one is cut so the words around the error always fit. */
const MAX_WHERE_BYTES = 256;
const CUT_MARK = '[earlier output cut]';
const ELLIPSIS = '…';

function question(where: string, message: string, detail: string | null): string {
  const details = detail === null ? '' : `Details:\n${detail}\n\n`;
  return `I got this error in the VibeSys app (${where}):\n\n${message}\n\n${details}Please look through this run's logs, find what caused it, and tell me what to do about it.`;
}

function codePointBytes(codePoint: number): number {
  if (codePoint < 0x80) return 1;
  if (codePoint < 0x800) return 2;
  return codePoint < 0x10000 ? 3 : 4;
}

/** The UTF-8 length of `text`, counted by code point (a lone surrogate encodes as three bytes). */
function utf8Bytes(text: string): number {
  let total = 0;
  for (const character of text) total += codePointBytes(character.codePointAt(0) ?? 0);
  return total;
}

/** The longest start of `text`, whole code points only, within `budget` bytes. */
function prefixWithin(text: string, budget: number): string {
  let used = 0;
  let end = 0;
  for (const character of text) {
    used += codePointBytes(character.codePointAt(0) ?? 0);
    if (used > budget) break;
    end += character.length;
  }
  return text.slice(0, end);
}

/** The longest end of `text`, whole code points only, within `budget` bytes. */
function suffixWithin(text: string, budget: number): string {
  const characters = Array.from(text);
  let used = 0;
  let start = characters.length;
  while (start > 0) {
    used += codePointBytes(characters[start - 1]?.codePointAt(0) ?? 0);
    if (used > budget) break;
    start -= 1;
  }
  return characters.slice(start).join('');
}

/**
 * The question that hands `report` to the run chat, at most `maxBytes` UTF-8 bytes (the host's cap
 * on a chat message). Cut to fit, it keeps the end of the diagnostic (where a failure's cause
 * usually is) after a marker; if even the message alone is too long, it keeps the message's start.
 */
export function errorQuestion(report: ErrorReport, maxBytes: number): string {
  if (maxBytes < MIN_QUESTION_BYTES)
    throw new RangeError(`a question needs at least ${MIN_QUESTION_BYTES} bytes, not ${maxBytes}`);
  const where =
    utf8Bytes(report.where) <= MAX_WHERE_BYTES
      ? report.where
      : `${prefixWithin(report.where, MAX_WHERE_BYTES)}${ELLIPSIS}`;
  const whole = question(where, report.message, report.detail);
  if (utf8Bytes(whole) <= maxBytes) return whole;
  if (report.detail !== null) {
    const frame = utf8Bytes(question(where, report.message, `${CUT_MARK}\n`));
    const tail = suffixWithin(report.detail, maxBytes - frame);
    if (tail !== '') return question(where, report.message, `${CUT_MARK}\n${tail}`);
  }
  const bare = question(where, report.message, null);
  if (utf8Bytes(bare) <= maxBytes) return bare;
  const frame = utf8Bytes(question(where, ELLIPSIS, null));
  return question(where, `${prefixWithin(report.message, maxBytes - frame)}${ELLIPSIS}`, null);
}
