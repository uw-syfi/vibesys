/** The words the run chat says, apart from the components that show them. */
import type {ChatFailure} from './failure.js';

/** The closed disclosure that holds a failure's diagnostic. */
export const DETAILS_LABEL = 'Details';
/** The notice under an answer whose turn failed. */
export const FAILED_ANSWER = 'Failed';
/** The notice under an answer the agent is still writing. */
export const WRITING_ANSWER = 'Writing';
/** The action that hands an error to the run chat, in a new thread. */
export const ASK_ABOUT_ERROR = 'Ask about this error';

/** An error as the run chat is asked about it: where it happened, what it said, and its diagnostic. */
export interface ErrorReport {
  readonly where: string;
  readonly message: string;
  readonly detail: string | null;
}

export function chatFailureReport(failure: ChatFailure): ErrorReport {
  return {where: 'Run chat', ...failure};
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
