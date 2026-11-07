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

/**
 * Every container of neighbours that can be hovered or chosen, by the file and selector that lays
 * them out. A new grouped control is added here, which is the point: its gap is the shared one.
 */
const GROUPS = [
  ['surfaces.css', '.vs-tab-list'],
  ['surfaces.css', '.vs-tree'],
  ['inputs.css', '.vs-segmented'],
  ['rail.css', '.vs-rail-group'],
  ['navigation.css', '.vs-sidebar-section'],
  ['navigation.css', '.vs-sidebar-group'],
  ['frame.css', '.vs-picklist'],
  ['base.css', '.vs-section-nav'],
  ['base.css', '.vs-section-steps'],
] as const;

describe('grouped controls', () => {
  test.each(GROUPS)('%s %s keeps the shared gap between its neighbours', (file, selector) => {
    expect(declarations(file, selector)).toContain('gap: var(--group-gap);');
  });

  test('a dense screen does not take it back', () => {
    const source = css('frame.css');
    const at = source.indexOf('.vs-screen[data-density="compact"] .vs-picklist {');
    expect(source.slice(at, source.indexOf('}', at))).toContain('gap: var(--group-gap);');
  });

  test('the shared gap is at least the second step of the spacing scale', () => {
    const base = css('base.css');
    expect(base).toMatch(/--group-gap: var\(--space-(2|3|4|5)\);/);
  });
});

describe('the window drag regions', () => {
  test('carve controls out of every region that drags', () => {
    expect(css('rail.css')).toContain('.vs-window-frame :is(a, button, input');
    expect(css('navigation.css')).toContain('.vs-app-nav :is(a, button');
    expect(css('navigation.css')).toContain('.vs-app-rail :is(a, button');
    expect(css('primitives.css')).toContain('-webkit-app-region: no-drag');
  });

  test('give the top band the height of what the window controls cover', () => {
    expect(declarations('rail.css', '.vs-window-drag')).toContain('--chrome-start-height');
  });
});
