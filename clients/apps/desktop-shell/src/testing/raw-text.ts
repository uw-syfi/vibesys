/**
 * What makes text on a screen read as code: the judgement of the raw-text check, in one place for
 * both tiers that hold it. The browser journeys send each function to the page by source
 * (`e2e/checks/in-page.ts` joins them), and the DOM tests of the shell call them directly over
 * the text a window draws. So each function here is self-contained: it uses only the DOM, its
 * own arguments and the other functions of this file, and no import.
 */

/** The words a check reads from the page: the run's closed sets that must never show raw. */
export interface RawTextInput {
  /** Enum values that never read as words (`implementation-failed`). */
  readonly rawWords: readonly string[];
  /** The ids the backend registries name (harnesses, models, roles): none may be drawn as text. */
  readonly ids: readonly string[];
  /** The names the registries give them: none may be drawn in lowercase. */
  readonly names: readonly string[];
  /** Names the user typed (a folder, an SSH alias): identifiers of theirs, not the app's code names. */
  readonly userNames: readonly string[];
}

/** One text a screen draws, and the element that draws it. */
export interface DrawnText {
  readonly element: Element;
  readonly text: string;
}

/** What is wrong with one text: the offending words, and where it is drawn. */
export interface RawTextFinding {
  readonly message: string;
  readonly where: string;
}

/** Whether `element` is code a person reads as code: identifiers are allowed there. */
function inCode(element: Element): boolean {
  return element.closest('code, pre, kbd, samp, script, style, textarea, [data-mono-id]') !== null;
}

function describe(element: Element): string {
  const classes = [...element.classList].slice(0, 2).join('.');
  const text = (element.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 40);
  return `${element.tagName.toLowerCase()}${classes === '' ? '' : `.${classes}`} "${text}"`;
}

/**
 * The labels in a text: its parts between the separators the screens join labels with (`·`, a
 * comma, a colon, parentheses), so a code name standing as a label is told from a hyphenated
 * word inside a sentence ("per-slot sequence numbers").
 */
function labelsOf(text: string): string[] {
  return text
    .split(/\s[·•|/+]\s|,\s|:\s|\s\(|\)/)
    .map(part => part.trim())
    .filter(part => part !== '');
}

/** Whether `text` has `id` standing as its own word, not inside a longer identifier. */
function hasIdAsWord(text: string, id: string): boolean {
  const escaped = id.replace(/[.*+?^${}()|[\]\\/-]/g, '\\$&');
  return new RegExp(`(?<![\\w.\\-/])${escaped}(?![\\w\\-/]|\\.\\w)`).test(text);
}

/** The registry ids that stand as a label of `text`: a label is a few words, a longer run is a sentence. */
function idHits(text: string, input: RawTextInput): string[] {
  const labels = labelsOf(text).filter(label => label.split(' ').length <= 4);
  return input.ids
    .filter(id => labels.some(label => hasIdAsWord(label, id)))
    .map(id => `registry id "${id}"`);
}

/** The labels of `text` that are a kebab-case code name, or a registry name drawn in lowercase. */
function nameHits(text: string, input: RawTextInput): string[] {
  const lowered = new Set(input.names.map(name => name.toLowerCase()));
  return labelsOf(text).flatMap(label => {
    if (/^[a-z]+(?:-[a-z0-9]+)+$/.test(label)) {
      return input.userNames.includes(label) ? [] : [`code name "${label}"`];
    }
    return lowered.has(label) && !input.names.includes(label) ? [`lowercase name "${label}"`] : [];
  });
}

/**
 * Code shown to a person: snake_case, template holes, undefined, NaN, null, long hashes, raw enums,
 * and the code names of the registries: a harness, model or role id drawn as text, a kebab-case
 * identifier standing as a label (`opus-5-5`), and a registry name drawn in lowercase as a label
 * (`claude code`). Ids are matched in labels (at most four words between separators), not in
 * sentences. Genuine identifiers a person may copy sit in a `data-mono-id` element.
 */
export function rawTextIn(boxes: readonly DrawnText[], input: RawTextInput): RawTextFinding[] {
  const patterns: [string, RegExp][] = [
    ['snake_case identifier', /\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b/],
    ['template hole', /\$\{/],
    ['undefined', /\bundefined\b/],
    ['NaN', /\bNaN\b/],
    ['null', /\bnull\b/],
    ['raw hash or id', /\b[0-9a-f]{12,}\b/i],
    ['raw markdown', /`|\*\*/],
  ];
  const found: RawTextFinding[] = [];
  for (const {element, text} of boxes) {
    if (inCode(element)) continue;
    const hits = [
      ...patterns.flatMap(([name, pattern]) => {
        const match = pattern.exec(text);
        return match === null ? [] : [`${name} "${match[0]}"`];
      }),
      ...input.rawWords.filter(word => text.includes(word)).map(word => `enum value "${word}"`),
      ...idHits(text, input),
      // An agent's own prose (rendered markdown) lists hyphenated words between commas: not labels.
      ...(element.closest('.vs-markdown') === null ? nameHits(text, input) : []),
    ];
    for (const hit of [...new Set(hits)]) {
      found.push({
        message: `raw text shown: ${hit} in "${text.slice(0, 50)}"`,
        where: describe(element),
      });
    }
  }
  return found;
}

/** The functions above, in the order the page's script defines them. */
export const RAW_TEXT_HELPERS: readonly ((...args: never[]) => unknown)[] = [
  inCode,
  describe,
  labelsOf,
  hasIdAsWord,
  idHits,
  nameHits,
  rawTextIn,
];
