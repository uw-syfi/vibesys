import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {
  LEGACY_ALIASES,
  renderTokensCss,
  TOKENS_CSS_COMMAND,
  TOKENS_CSS_PATH,
  tokensCssDrift,
} from './css.js';
import {
  CHIP_FILL_MIX,
  COLOR_ROLES,
  CONTRAST_PAIRS,
  contrastRatio,
  DEFAULT_THEMES,
  LOG_ERR_MIX,
  MOTION,
  mixHex,
  TEXT_FLOOR,
  THEMES,
  type ThemeMode,
  type ThemeName,
} from './index.js';

const THEME_NAMES = Object.keys(THEMES) as ThemeName[];
const read = (name: string): string => readFileSync(new URL(`../${name}`, import.meta.url), 'utf8');

describe('THEMES', () => {
  test('are the six themes, each with a mode and a default per mode', () => {
    expect(THEME_NAMES.sort()).toEqual(['ink', 'midnight', 'mist', 'paper', 'sand', 'terminal']);
    for (const mode of ['light', 'dark'] as const satisfies readonly ThemeMode[]) {
      expect(THEMES[DEFAULT_THEMES[mode]].mode).toBe(mode);
    }
  });

  test('define every role as a hex color', () => {
    for (const name of THEME_NAMES) {
      expect(Object.keys(THEMES[name].colors).sort()).toEqual([...COLOR_ROLES].sort());
      for (const value of Object.values(THEMES[name].colors)) {
        expect(value).toMatch(/^#[0-9a-f]{6}([0-9a-f]{2})?$/);
      }
    }
  });

  test('draw the chart grid as the text color at 6 to 7 percent alpha', () => {
    for (const name of THEME_NAMES) {
      const {text, grid} = THEMES[name].colors;
      expect(grid.slice(0, 7)).toBe(text);
      const alpha = Number.parseInt(grid.slice(7), 16) / 255;
      expect(alpha).toBeGreaterThanOrEqual(0.06);
      expect(alpha).toBeLessThanOrEqual(0.07);
    }
  });

  test('keep the log pane dark in every theme', () => {
    for (const name of THEME_NAMES) {
      expect(contrastRatio(THEMES[name].colors['log-bg'], '#000000')).toBeLessThan(2);
    }
  });
});

describe('contrastRatio', () => {
  const SAMPLES = ['#000000', '#ffffff', '#2f5bea', '#167a3a', '#f7f6f3', '#808080'];

  test('is 21 for black on white, 1 for a color on itself, and symmetric', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5);
    for (const left of SAMPLES) {
      expect(contrastRatio(left, left)).toBeCloseTo(1, 10);
      for (const right of SAMPLES) {
        expect(contrastRatio(left, right)).toBe(contrastRatio(right, left));
        expect(contrastRatio(left, right)).toBeGreaterThanOrEqual(1);
      }
    }
  });

  test('reads shorthand and alpha forms as the color they stand for', () => {
    expect(contrastRatio('#fff', '#000')).toBeCloseTo(21, 5);
    expect(contrastRatio('#ffffff80', '#000000')).toBeCloseTo(21, 5);
  });

  test('rejects what is not a hex color', () => {
    expect(() => contrastRatio('red', '#ffffff')).toThrow('Invalid hex color: red');
  });
});

describe('contrast floors', () => {
  test('test_every_theme_meets_contrast_floors', () => {
    const failures = THEME_NAMES.flatMap(name =>
      CONTRAST_PAIRS.flatMap(({foreground, background, floor, use}) => {
        const {colors} = THEMES[name];
        const ratio = contrastRatio(colors[foreground], colors[background]);
        return ratio >= floor
          ? []
          : [
              `${name}: ${foreground} on ${background} (${use}) is ${ratio.toFixed(2)}:1, below ${floor}:1`,
            ];
      }),
    );
    expect(failures).toEqual([]);
  });

  test('a chip reads on its own fill, not only on the panel, in every theme', () => {
    const {share, into} = CHIP_FILL_MIX;
    const failures = THEME_NAMES.flatMap(name => {
      const {colors} = THEMES[name];
      return (['accent', 'ok', 'warn', 'err'] as const).flatMap(tone => {
        const ratio = contrastRatio(colors[tone], mixHex(colors[tone], share, colors[into]));
        return ratio >= TEXT_FLOOR ? [] : [`${name}: ${tone} chip text is ${ratio.toFixed(2)}:1`];
      });
    });
    expect(failures).toEqual([]);
  });

  test('standard error in the log pane reads on the log background in every theme', () => {
    const {role, share, into, on} = LOG_ERR_MIX;
    const failures = THEME_NAMES.flatMap(name => {
      const {colors} = THEMES[name];
      const ratio = contrastRatio(mixHex(colors[role], share, colors[into]), colors[on]);
      return ratio >= TEXT_FLOOR ? [] : [`${name}: --log-err on ${on} is ${ratio.toFixed(2)}:1`];
    });
    expect(failures).toEqual([]);
  });
});

describe('renderTokensCss', () => {
  const css = renderTokensCss('/* primitives */');
  const blockOf = (name: ThemeName): string => {
    const start = css.indexOf(`:root[data-theme="${name}"]`);
    return css.slice(start, css.indexOf('}', start));
  };

  test('has one block per theme carrying every role and the theme mode', () => {
    for (const name of THEME_NAMES) {
      const block = blockOf(name);
      expect(block).toContain(`color-scheme: ${THEMES[name].mode};`);
      for (const role of COLOR_ROLES) {
        expect(block).toContain(`--${role}: ${THEMES[name].colors[role]};`);
      }
    }
  });

  test('puts the defaults on a bare :root, light, then dark under the OS preference', () => {
    expect(css).toContain(`:root {\n  color-scheme: light;\n  --bg: ${THEMES.paper.colors.bg};`);
    const dark = css.slice(css.indexOf('@media (prefers-color-scheme: dark)'));
    expect(dark).toContain(`:root {\n    color-scheme: dark;\n    --bg: ${THEMES.ink.colors.bg};`);
    expect(css).not.toContain(':not([data-theme])');
  });

  test('derives fills from the roles with color-mix, never from literals', () => {
    expect(css).toContain('--selection: color-mix(in srgb, var(--accent)');
    for (const tone of ['accent', 'ok', 'warn', 'err']) {
      expect(css).toContain(`--chip-${tone}-bg: color-mix(`);
      expect(css).toContain(`--chip-${tone}-border: color-mix(`);
      expect(css).toContain(`--banner-${tone}-bg: color-mix(`);
    }
  });

  test('zeroes every motion duration under reduced motion', () => {
    const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
    for (const token of Object.keys(MOTION.duration)) {
      expect(reduced).toContain(`--duration-${token}: 0ms;`);
    }
  });

  test('keeps the old variable names resolving to the new roles', () => {
    const OLD_NAMES = [
      'page',
      'panel',
      'line',
      'text',
      'muted',
      'accent',
      'accent-text',
      'hover',
      'selected',
      'error',
      'warn',
      'ok',
      'titlebar',
    ];
    for (const name of OLD_NAMES) {
      const named = css.includes(`--${name}:`);
      expect(named).toBe(true);
    }
    for (const [name, value] of Object.entries(LEGACY_ALIASES)) {
      expect(css).toContain(`--${name}: ${value};`);
    }
  });

  test('ends with the primitives, and is a pure function of them', () => {
    expect(css.startsWith('/*\n * GENERATED')).toBe(true);
    expect(css.endsWith('/* primitives */\n')).toBe(true);
    expect(renderTokensCss('/* primitives */')).toBe(css);
  });
});

describe('tokensCssDrift', () => {
  const primitives = read('primitives.css');
  const current = read('tokens.css');

  test('finds the checked-in tokens.css current', () => {
    expect(tokensCssDrift(current, primitives)).toBeNull();
  });

  test('fails a hand edit, naming the file and the command that fixes it', () => {
    const edits = [
      current.replace('#f7f6f3', '#f7f6f4'),
      `${current}.extra { color: red; }\n`,
      current.slice(1),
    ];
    for (const edited of edits) {
      const message = tokensCssDrift(edited, primitives);
      expect(message).toContain(TOKENS_CSS_PATH);
      expect(message).toContain(TOKENS_CSS_COMMAND);
    }
  });
});

interface Rule {
  readonly dark: boolean;
  readonly attribute: string | null;
  readonly declarations: ReadonlyMap<string, string>;
}

/** The rules of `css` that set a declaration on `:root`, with the data-theme each one requires. */
function rootRules(css: string): Rule[] {
  const rules: Rule[] = [];
  for (const [, media, selector = '', body = ''] of css.matchAll(
    /(?:@media \(prefers-color-scheme: (dark)\) \{\s*)?(:root(?:\[data-theme="[a-z]+"\])?) \{([^}]*)\}/g,
  )) {
    const attribute = /\[data-theme="([a-z]+)"\]/.exec(selector)?.[1] ?? null;
    const declarations = new Map(
      [...body.matchAll(/(--[\w-]+|color-scheme): ([^;]+);/g)].map(m => [m[1] ?? '', m[2] ?? '']),
    );
    rules.push({dark: media === 'dark', attribute, declarations});
  }
  return rules;
}

describe('the cascade of renderTokensCss', () => {
  const rules = rootRules(renderTokensCss(''));

  /** The value of each role on `:root` for a data-theme and an OS preference: the cascade. */
  function resolve(dataTheme: string | null, osDark: boolean): Map<string, string> {
    const applies = rules.filter(
      rule => (!rule.dark || osDark) && (rule.attribute === null || rule.attribute === dataTheme),
    );
    // Specificity first (a named block beats a bare :root), then source order.
    const ordered = [...applies].sort(
      (a, b) => Number(a.attribute !== null) - Number(b.attribute !== null),
    );
    return new Map(ordered.flatMap(rule => [...rule.declarations]));
  }

  test('defines every role for any data-theme outside ThemeName, following the OS', () => {
    const unknown = [
      null,
      '',
      'dark',
      'light',
      'catppuccin-mocha',
      'solarized-light',
      'PAPER',
      'x',
    ];
    for (const dataTheme of unknown) {
      expect(dataTheme === null || !(dataTheme in THEMES)).toBe(true);
      for (const osDark of [false, true]) {
        const resolved = resolve(dataTheme, osDark);
        const expected = THEMES[osDark ? DEFAULT_THEMES.dark : DEFAULT_THEMES.light];
        for (const role of COLOR_ROLES)
          expect(resolved.get(`--${role}`)).toBe(expected.colors[role]);
      }
    }
  });

  test('lets a named theme win over the OS preference', () => {
    for (const name of THEME_NAMES) {
      for (const osDark of [false, true]) {
        const resolved = resolve(name, osDark);
        for (const role of COLOR_ROLES) {
          expect(resolved.get(`--${role}`)).toBe(THEMES[name].colors[role]);
        }
      }
    }
  });
});
