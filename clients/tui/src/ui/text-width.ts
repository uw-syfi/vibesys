/**
 * Terminal cell measurement, for code that has a column budget to spend.
 *
 * A terminal lays text out in cells, not UTF-16 code units, and the two differ
 * in both directions: a CJK ideograph is one code unit and two cells, a
 * combining mark is one code unit and no cell, and an emoji is several code
 * units that occupy two cells together. Budgeting with `String.length` makes a
 * line the caller believes is 40 columns take 50, and slicing by code units
 * can cut a surrogate pair or a joined emoji sequence in half.
 *
 * The rules here are the ones terminals actually apply: East Asian Wide and
 * Fullwidth are two cells, so is anything with an emoji presentation, marks
 * and format characters are none, everything else is one.
 */

/** Grapheme clusters, so a slice never lands inside one. */
const GRAPHEMES = new Intl.Segmenter('en', {granularity: 'grapheme'});

/**
 * Code point ranges that occupy two cells: East Asian Wide and Fullwidth,
 * which JS exposes no property escape for. Emoji are covered separately by
 * `Emoji_Presentation`, which is a property escape.
 */
const WIDE_RANGES: readonly (readonly [number, number])[] = [
  [0x1100, 0x115f], // Hangul Jamo initial consonants
  [0x2e80, 0x303e], // CJK radicals, Kangxi, CJK symbols and punctuation
  [0x3041, 0x33ff], // Kana, Hangul Compatibility Jamo, CJK compatibility
  [0x3400, 0x4dbf], // CJK Unified Ideographs Extension A
  [0x4e00, 0x9fff], // CJK Unified Ideographs
  [0xa000, 0xa4cf], // Yi syllables and radicals
  [0xa960, 0xa97f], // Hangul Jamo Extended-A
  [0xac00, 0xd7a3], // Hangul syllables
  [0xf900, 0xfaff], // CJK compatibility ideographs
  [0xfe10, 0xfe19], // Vertical forms
  [0xfe30, 0xfe6f], // CJK compatibility forms, small form variants
  [0xff00, 0xff60], // Fullwidth forms
  [0xffe0, 0xffe6], // Fullwidth signs
  [0x17000, 0x18aff], // Tangut
  [0x20000, 0x3fffd], // CJK Unified Ideographs, Extension B onward
];

/** Two cells without asking: the code point's default form is the emoji one. */
const EMOJI_PRESENTATION = /\p{Emoji_Presentation}/u;

/** U+FE0F asks for the emoji form of a code point that defaults to text. */
const EMOJI_VARIATION_SELECTOR = '\uFE0F';

/**
 * No cell of its own: combining marks stack onto the previous character and
 * format characters (joiners, variation selectors) are instructions, not glyphs.
 */
const ZERO_WIDTH = /^[\p{Mn}\p{Me}\p{Cf}]$/u;

/** Cells `text` occupies in a terminal. */
export function displayWidth(text: string): number {
  let width = 0;
  for (const {segment} of GRAPHEMES.segment(text)) width += graphemeWidth(segment);
  return width;
}

/**
 * The longest prefix of `text` that fits `maxWidth` cells, cut on a grapheme
 * boundary.
 *
 * A wide grapheme that would straddle the limit is dropped whole, so the
 * result is sometimes one cell narrower than the budget. That is the point:
 * half of a grapheme is not half a character, it is a broken one.
 */
export function truncateToWidth(text: string, maxWidth: number): string {
  if (maxWidth <= 0) return '';
  let width = 0;
  let end = 0;
  for (const {segment, index} of GRAPHEMES.segment(text)) {
    const next = width + graphemeWidth(segment);
    if (next > maxWidth) break;
    width = next;
    end = index + segment.length;
  }
  return text.slice(0, end);
}

/**
 * `text` followed by the spaces that bring it to exactly `width` cells.
 *
 * The column-alignment counterpart of `truncateToWidth`: `String.padEnd`
 * counts code units, so a cell holding CJK text comes out short of its column
 * and pushes every later column out of line. Text already at or past the
 * budget is returned unchanged; trimming it is `truncateToWidth`'s job.
 */
export function padToWidth(text: string, width: number): string {
  return text + ' '.repeat(Math.max(0, width - displayWidth(text)));
}

/**
 * A cluster is as wide as the character it is built around: the code points
 * after the first are marks, joiners and selectors that render into the same
 * cells.
 */
function graphemeWidth(cluster: string): number {
  const first = cluster.codePointAt(0);
  if (first === undefined) return 0;
  const base = String.fromCodePoint(first);
  if (ZERO_WIDTH.test(base)) return 0;
  if (EMOJI_PRESENTATION.test(base)) return 2;
  if (cluster.includes(EMOJI_VARIATION_SELECTOR)) return 2;
  return isWide(first) ? 2 : 1;
}

function isWide(codePoint: number): boolean {
  return WIDE_RANGES.some(([start, end]) => codePoint >= start && codePoint <= end);
}
