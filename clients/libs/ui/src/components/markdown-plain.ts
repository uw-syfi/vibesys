/** A fence, a rule or a table's divider row: a line that is only markup, with no words. */
const MARKUP_ONLY_LINE = /^\s*(?:[-*_]{3,}|[|:\s-]*-[|:\s-]*\|[|:\s-]*|`{3,}|~{3,})\s*$/;
/** What starts a line as a block: a quote, a heading, a bullet, a number, a checkbox. */
const BLOCK_START =
  /^\s*(?:>+\s*|#{1,6}(?:\s+|$)|[-*+](?:\s+|$)(?:\[[ xX]\]\s+)?|\d+[.)](?:\s+|$))/;
const IMAGE_OR_LINK = /!?\[([^\]\n]*)\](?:\([^)\n]*\)|\[[^\]\n]*\])/g;
const AUTOLINK = /<((?:https?:\/\/|mailto:)[^>\s]+)>/g;
const ESCAPE = /\\([\\`*_{}[\]()#+\-.!|~>])/g;
/** `_x_` and `__x__` at word edges: an underscore inside a word is not emphasis. */
const UNDERSCORE_EMPHASIS = /(^|[^\w])_{1,2}([^_\n]+?)_{1,2}(?=[^\w]|$)/g;
const STRAY_MARKS = /\*+|`+|~~+/g;

/** One pass over `text`: every construct it knows is replaced by the words it holds. */
function pass(text: string): string {
  return text
    .split('\n')
    .filter(line => !MARKUP_ONLY_LINE.test(line))
    .map(line => line.replace(BLOCK_START, '').replaceAll('|', ' '))
    .join(' ')
    .replace(IMAGE_OR_LINK, '$1')
    .replace(AUTOLINK, '$1')
    .replace(ESCAPE, '$1')
    .replace(UNDERSCORE_EMPHASIS, '$1$2')
    .replace(STRAY_MARKS, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Markdown as one line of plain words, for a row summary or a preview: emphasis, code, links,
 * images, headings, quotes, bullets, tables and fences are replaced by what they hold, lines are
 * joined by a space, and a mark left unpaired (a preview cut mid-emphasis) is dropped. Text with
 * no markup comes back with only its whitespace collapsed. It repeats until nothing changes, so
 * its output is itself plain and `markdownToPlain` of it is the same.
 */
export function markdownToPlain(text: string): string {
  let current = text;
  for (;;) {
    const next = pass(current);
    if (next === current) return next;
    current = next;
  }
}
