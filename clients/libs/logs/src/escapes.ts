/**
 * Terminal escape sequences in log text: a tool writing to what it takes for a terminal colors its
 * output (SGR, `ESC [ 33 m`), moves the cursor or sets a title, and a log view renders text only.
 * `escapeStep` is the scanner, one character at a time, so a sequence a chunk boundary cuts is
 * still recognized: the fold carries its state (`LogView.escape`) to the next chunk.
 *
 * Recognized, and dropped whole:
 *
 * - CSI: `ESC [`, parameter and intermediate bytes (U+0020 to U+003F), one final byte (U+0040 to
 *   U+007E). SGR colors are CSI sequences ending in `m`.
 * - OSC: `ESC ]` up to BEL or the string terminator `ESC \`.
 * - Other escapes: `ESC`, intermediate bytes (U+0020 to U+002F), one final byte.
 *
 * A character a sequence cannot hold ends it and is kept as text, and so does a line end, so a
 * malformed sequence swallows at most its own prefix. Every other character is kept.
 */

/** Where the scanner is: in text, or inside a sequence it has not finished. */
export type EscapeState = 'text' | 'escape' | 'csi' | 'osc' | 'osc-escape';

const ESC = '\u001b';
const BEL = '\u0007';

function within(character: string, low: number, high: number): boolean {
  const code = character.codePointAt(0) ?? -1;
  return code >= low && code <= high;
}

type Step = {readonly next: EscapeState; readonly keep: boolean};

/** A final byte ends the sequence and is dropped; any other character ends it and is kept. */
function ended(character: string, low: number): Step {
  return {next: 'text', keep: !within(character, low, 0x7e)};
}

function afterEscape(character: string): Step {
  if (character === '[') return {next: 'csi', keep: false};
  if (character === ']') return {next: 'osc', keep: false};
  return within(character, 0x20, 0x2f) ? {next: 'escape', keep: false} : ended(character, 0x30);
}

/** Inside a sequence: the state after `character`, and whether it ends the sequence as text. */
function inSequence(state: Exclude<EscapeState, 'text'>, character: string): Step {
  switch (state) {
    case 'escape':
      return afterEscape(character);
    case 'csi':
      return within(character, 0x20, 0x3f) ? {next: 'csi', keep: false} : ended(character, 0x40);
    case 'osc':
      if (character === BEL) return {next: 'text', keep: false};
      return {next: character === ESC ? 'osc-escape' : 'osc', keep: false};
    case 'osc-escape':
      return {next: character === '\\' ? 'text' : 'osc', keep: false};
  }
}

/**
 * One character through the scanner: the state after it, and whether it is text to keep. A line
 * end always ends a sequence and is kept, so a sequence never spans lines.
 */
export function escapeStep(
  state: EscapeState,
  character: string,
): {readonly next: EscapeState; readonly keep: boolean} {
  if (character === '\n') return {next: 'text', keep: true};
  if (state === 'text') {
    return character === ESC ? {next: 'escape', keep: false} : {next: 'text', keep: true};
  }
  const step = inSequence(state, character);
  // A kept character ended a malformed sequence; it may itself start the next one.
  return step.keep && character === ESC ? {next: 'escape', keep: false} : step;
}

/** `text` without its escape sequences; an unfinished sequence at its end is dropped too. */
export function stripEscapes(text: string): string {
  let state: EscapeState = 'text';
  let kept = '';
  for (const character of text) {
    const step = escapeStep(state, character);
    state = step.next;
    if (step.keep) kept += character;
  }
  return kept;
}
