/**
 * Split a line of run arguments the way a POSIX shell splits words, without expanding anything:
 * blanks separate words, single quotes keep everything literally, double quotes keep everything but
 * `\"`, `\\`, `\$`, and `` \` ``, and a backslash outside quotes keeps the next character. The
 * picker's run-arguments field uses it, so `--goal "make it fast"` is two arguments, not four.
 */
export class ShellWordsError extends Error {
  override name = 'ShellWordsError';
}

/** The words of `line`; throws `ShellWordsError` on an unclosed quote or a trailing `\`. */
export function shellWords(line: string): string[] {
  const words: string[] = [];
  let word: string | null = null;
  let index = 0;
  while (index < line.length) {
    if (/\s/.test(line[index] as string)) {
      if (word !== null) words.push(word);
      word = null;
      index += 1;
      continue;
    }
    const [text, next] = token(line, index);
    word = (word ?? '') + text;
    index = next;
  }
  if (word !== null) words.push(word);
  return words;
}

/** The text of the token at `index` (a quoted run, an escape, or one character) and what follows. */
function token(line: string, index: number): [string, number] {
  const char = line[index] as string;
  if (char === "'") {
    const end = line.indexOf("'", index + 1);
    if (end < 0) throw new ShellWordsError("the arguments have an unclosed '");
    return [line.slice(index + 1, end), end + 1];
  }
  if (char === '"') return doubleQuoted(line, index + 1);
  if (char === '\\') {
    if (index + 1 >= line.length) throw new ShellWordsError('the arguments end with a lone \\');
    return [line[index + 1] as string, index + 2];
  }
  return [char, index + 1];
}

function doubleQuoted(line: string, start: number): [string, number] {
  let text = '';
  for (let index = start; index < line.length; index += 1) {
    const char = line[index] as string;
    if (char === '"') return [text, index + 1];
    if (char === '\\' && /["\\$`]/.test(line[index + 1] ?? '')) {
      index += 1;
      text += line[index];
    } else {
      text += char;
    }
  }
  throw new ShellWordsError('the arguments have an unclosed "');
}
