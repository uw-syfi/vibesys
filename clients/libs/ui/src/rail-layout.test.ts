import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';

const css = (name: string): string => readFileSync(new URL(name, import.meta.url), 'utf8');

/** The declarations of the first rule whose selector is exactly `selector`. */
function declarations(file: string, selector: string): string {
  const source = css(file);
  const at = source.indexOf(`\n${selector} {`);
  if (at < 0) throw new Error(`${file} has no rule for ${selector}`);
  return source.slice(at, source.indexOf('}', at));
}

const px = (name: string): number => {
  const found = new RegExp(`--${name}: (\\d+)px;`).exec(css('tokens.css'));
  if (found === null) throw new Error(`no --${name} token`);
  return Number(found[1]);
};

/** The rail's items are labelled, so the rail must be as wide as its longest label needs. */
const LABELS = ['Runs', 'Cost', 'Machines', 'Settings'];
/** The widest an average caption glyph at medium weight is, in px. */
const GLYPH_PX = 6;

describe('the rail', () => {
  test('is wide enough for its longest label on one line, beside the item padding', () => {
    const longest = Math.max(...LABELS.map(label => label.length)) * GLYPH_PX;
    const side = px('space-1');
    expect(px('rail') - 2 * side - 2).toBeGreaterThanOrEqual(longest);
  });

  test('holds its foot group at the end with a spacer, and never scrolls: the window opens no shorter than its groups', () => {
    expect(declarations('rail.css', '.vs-rail')).not.toContain('overflow');
    expect(declarations('rail.css', '.vs-rail')).not.toContain('justify-content');
    expect(declarations('rail.css', '.vs-rail-group')).toContain('flex: none;');
    // The spacer grows into the free room and never shrinks below one step.
    expect(declarations('base.css', '.vs-spacer')).toContain('flex: 1 0 var(--space-4);');
  });

  test('never wraps a label', () => {
    expect(declarations('rail.css', '.vs-rail-item .vs-text')).toContain('white-space: nowrap;');
  });

  test('keeps its items apart by the shared gap, so a hover and a selection never touch', () => {
    expect(declarations('rail.css', '.vs-rail-group')).toContain('gap: var(--group-gap);');
    const item = declarations('rail.css', '.vs-rail-item');
    expect(item).toContain('margin: 0;');
    expect(item).not.toContain('box-shadow');
  });

  test('draws the selection as a tint, without a border or an outline', () => {
    const selected = declarations('rail.css', '.vs-rail-item[data-active]');
    expect(selected).toContain('background: var(--rail-selected);');
    expect(selected).not.toMatch(/border|outline|box-shadow/);
    expect(declarations('rail.css', '.vs-rail-item[data-active] .vs-rail-icon')).toContain(
      'color: var(--accent);',
    );
    expect(declarations('rail.css', '.vs-rail-item:hover')).toContain(
      'background: var(--rail-hover);',
    );
  });

  test('moves only through the motion tokens', () => {
    const item = declarations('rail.css', '.vs-rail-item');
    expect(item).toContain('var(--duration-fast) var(--easing)');
    expect(item).not.toMatch(/\d+m?s /);
  });

  test("draws its mark in an item's box with none of an item's states", () => {
    const mark = declarations('rail.css', '.vs-rail-mark-icon');
    const item = declarations('rail.css', '.vs-rail-item');
    for (const same of ['min-height: var(--control);', 'padding: var(--space-3) var(--space-0);']) {
      expect(mark).toContain(same);
      expect(item).toContain(same);
    }
    expect(mark).toContain('cursor: default;');
    // No hover, focus or press rule reaches the mark.
    expect(css('rail.css')).not.toMatch(/\.vs-rail-mark[^{]*:(hover|focus|active)/);
  });
});
