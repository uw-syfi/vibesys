/** Reassembles newline-delimited protocol frames across arbitrary socket chunks. */
export class NewlineFramer {
  #remainder = '';

  push(chunk: string): string[] {
    const lines = `${this.#remainder}${chunk}`.split('\n');
    this.#remainder = lines.pop() ?? '';
    return lines;
  }
}
