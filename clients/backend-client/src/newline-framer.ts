import {BackendClientError} from './errors.js';

/**
 * The largest newline-less remainder the framer will hold before it declares the
 * stream unreadable. Protocol messages are single JSON lines far below this, so
 * a remainder this large is a peer that has stopped delimiting frames (a corrupt
 * or hostile server), not a real message. The cap is generous on purpose: it
 * exists to bound memory against an endless chunk, not to police message size.
 */
const MAX_REMAINDER_CHARS = 4 * 1024 * 1024;

/** Reassembles newline-delimited protocol frames across arbitrary socket chunks. */
export class NewlineFramer {
  #remainder = '';

  push(chunk: string): string[] {
    const lines = `${this.#remainder}${chunk}`.split('\n');
    this.#remainder = lines.pop() ?? '';
    if (this.#remainder.length > MAX_REMAINDER_CHARS) {
      // Drop the buffer before throwing: the stream is being torn down, and
      // holding the oversized remainder past the failure serves nothing.
      this.#remainder = '';
      throw new BackendClientError(
        'parse',
        `Server frame exceeded ${MAX_REMAINDER_CHARS} characters without a newline`,
      );
    }
    return lines;
  }
}
