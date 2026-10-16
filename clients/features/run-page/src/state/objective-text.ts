/**
 * The objective's Markdown as the Overview shows it: headings, paragraphs and lists only. Inline
 * markup (emphasis, code, links, images) is reduced to its text, and anything else (tables, code
 * fences, quotes, HTML) is shown as the plain text it contains, so nothing the task author wrote is
 * lost and nothing is interpreted as markup.
 */

export type ObjectiveBlock =
  | {readonly kind: 'heading'; readonly text: string}
  | {readonly kind: 'paragraph'; readonly text: string}
  | {readonly kind: 'list'; readonly ordered: boolean; readonly items: readonly string[]};

const HEADING = /^#{1,6}\s+(.*?)\s*#*\s*$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const FENCE = /^\s*(```|~~~)/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;

/** Inline Markdown reduced to its text. */
function plainInline(text: string): string {
  return text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/(\*\*|__)(.+?)\1/g, '$2')
    .replace(/(^|[^\w*])[*_]([^*_\s][^*_]*?)[*_](?=[^\w*]|$)/g, '$1$2')
    .replace(/<[^>]+>/g, '')
    .replace(/^\s*>\s?/, '')
    .replace(/\s+/g, ' ')
    .trim();
}

interface Builder {
  readonly blocks: ObjectiveBlock[];
  paragraph: string[];
  list: {ordered: boolean; items: string[]} | null;
}

function flush(builder: Builder): void {
  if (builder.paragraph.length > 0) {
    const text = plainInline(builder.paragraph.join(' '));
    if (text !== '') builder.blocks.push({kind: 'paragraph', text});
    builder.paragraph = [];
  }
  if (builder.list !== null) {
    builder.blocks.push({kind: 'list', ordered: builder.list.ordered, items: builder.list.items});
    builder.list = null;
  }
}

function listItem(builder: Builder, ordered: boolean, text: string): void {
  if (builder.paragraph.length > 0 || builder.list?.ordered !== ordered) flush(builder);
  builder.list ??= {ordered, items: []};
  builder.list.items.push(plainInline(text));
}

function continued(builder: Builder, line: string): boolean {
  if (builder.list === null || !/^\s+\S/.test(line)) return false;
  // An indented line continues the list item above it.
  const items = builder.list.items;
  items[items.length - 1] = plainInline(`${items.at(-1) ?? ''} ${line}`);
  return true;
}

function readLine(builder: Builder, line: string): void {
  if (line.trim() === '' || FENCE.test(line) || RULE.test(line)) {
    flush(builder);
    return;
  }
  const heading = HEADING.exec(line);
  if (heading !== null) {
    flush(builder);
    const text = plainInline(heading[1] ?? '');
    if (text !== '') builder.blocks.push({kind: 'heading', text});
    return;
  }
  const item = BULLET.exec(line) ?? NUMBERED.exec(line);
  if (item !== null) {
    listItem(builder, !BULLET.test(line), item[1] ?? '');
    return;
  }
  if (continued(builder, line)) return;
  if (builder.list !== null) flush(builder);
  builder.paragraph.push(line.replace(/^\s*\|/, '').replace(/\|\s*$/, ''));
}

/** The objective's blocks, in order. */
export function objectiveBlocks(markdown: string): readonly ObjectiveBlock[] {
  const builder: Builder = {blocks: [], paragraph: [], list: null};
  for (const line of markdown.split(/\r?\n/)) readLine(builder, line);
  flush(builder);
  return builder.blocks;
}
