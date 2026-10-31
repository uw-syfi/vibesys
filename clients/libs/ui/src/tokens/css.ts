import {CHIP_FILL_MIX, LOG_ERR_MIX} from './contrast.js';
import {
  ELEVATION,
  FONT_STACKS,
  MOTION,
  PROSE_MEASURE_CH,
  RADIUS,
  SERIES,
  SIZE,
  SPACE,
  type TextStyle,
  TYPE,
} from './scales.js';
import {COLOR_ROLES, DEFAULT_THEMES, THEMES, type ThemeName} from './themes.js';

/** Where `tokens.css` lives and how it is rewritten, for the messages that name them. */
export const TOKENS_CSS_PATH = 'clients/libs/ui/src/tokens.css';
export const TOKENS_CSS_COMMAND = 'pnpm --dir clients/libs/ui generate:tokens';

/**
 * The names the stylesheet had before the theme roles, kept until its consumers move to the roles.
 * A name that is also a role (`--panel`, `--line`, `--text`, `--muted`, `--accent`, `--ok`,
 * `--warn`) needs no alias.
 */
export const LEGACY_ALIASES: Readonly<Record<string, string>> = {
  page: 'var(--bg)',
  error: 'var(--err)',
  'accent-text': 'var(--bg)',
  hover: 'var(--panel-2)',
  selected: 'var(--selection)',
};

const TONES = ['accent', 'ok', 'warn', 'err'] as const;
/** How much of a role is mixed into its surface: selection, chip fill, chip border, banner fill. */
const MIX = {selection: 14, fill: CHIP_FILL_MIX.share, border: 35, banner: 8};

const HEADER = `/*
 * GENERATED, do not edit. Change the tables in src/tokens/ (or the primitives' styles in
 * src/primitives.css), then regenerate with \`${TOKENS_CSS_COMMAND}\`.
 */`;

function block(selector: string, declarations: readonly string[]): string {
  return `${selector} {\n${declarations.map(line => `  ${line}`).join('\n')}\n}`;
}

function mix(role: string, percent: number, into: string): string {
  return `color-mix(in srgb, var(--${role}) ${percent}%, var(--${into}))`;
}

function themeDeclarations(name: ThemeName): string[] {
  const {mode, colors} = THEMES[name];
  return [`color-scheme: ${mode};`, ...COLOR_ROLES.map(role => `--${role}: ${colors[role]};`)];
}

function textDeclarations(style: TextStyle): string[] {
  const spec = TYPE[style];
  const prefix = `--text-${style}`;
  return [
    `${prefix}-family: var(--font-${spec.family});`,
    `${prefix}-size: ${spec.size}px;`,
    `${prefix}-line: ${spec.line}px;`,
    `${prefix}-weight: ${spec.weight};`,
    `${prefix}-tracking: ${spec.tracking}em;`,
    `${prefix}-transform: ${spec.uppercase ? 'uppercase' : 'none'};`,
    ...(spec.color === null ? [] : [`${prefix}-color: var(--${spec.color});`]),
  ];
}

function scaleDeclarations(): string[] {
  return [
    ...Object.entries(FONT_STACKS).map(([family, stack]) => `--font-${family}: ${stack};`),
    ...(Object.keys(TYPE) as TextStyle[]).flatMap(textDeclarations),
    ...Object.entries(SPACE).map(([token, px]) => `--${token}: ${px}px;`),
    ...Object.entries(SIZE).map(([token, px]) => `--${token}: ${px}px;`),
    `--measure: ${PROSE_MEASURE_CH}ch;`,
    ...Object.entries(RADIUS).map(([token, px]) => `--${token}: ${px}px;`),
    ...Object.entries(ELEVATION).flatMap(([level, {shadow, scrim}]) => [
      `--elevation-${level}: ${shadow};`,
      ...(scrim === null ? [] : [`--scrim: ${scrim};`]),
    ]),
    ...Object.entries(MOTION.duration).map(([token, ms]) => `--duration-${token}: ${ms}ms;`),
    `--easing: ${MOTION.easing};`,
    ...SERIES.map((color, slot) => `--series-${slot}: ${color};`),
  ];
}

function derivedDeclarations(): string[] {
  return [
    `--selection: ${mix('accent', MIX.selection, 'bg')};`,
    `--log-err: ${mix(LOG_ERR_MIX.role, LOG_ERR_MIX.share, LOG_ERR_MIX.into)};`,
    ...TONES.flatMap(tone => [
      `--chip-${tone}-bg: ${mix(tone, MIX.fill, CHIP_FILL_MIX.into)};`,
      `--chip-${tone}-border: ${mix(tone, MIX.border, 'panel')};`,
      `--banner-${tone}-bg: ${mix(tone, MIX.banner, 'bg')};`,
    ]),
  ];
}

function media(query: string, rule: string): string {
  const indented = rule
    .split('\n')
    .map(line => `  ${line}`)
    .join('\n');
  return `@media (${query}) {\n${indented}\n}`;
}

function reducedMotion(): string {
  const zeroed = Object.keys(MOTION.duration).map(token => `--duration-${token}: 0ms;`);
  return media('prefers-reduced-motion: reduce', block(':root', zeroed));
}

function themeBlocks(): string[] {
  const names = Object.keys(THEMES) as ThemeName[];
  return [
    // The defaults sit on a bare `:root`, whatever `data-theme` holds (another app sets it to names
    // of its own), and follow the OS between the two default themes. A named block below is more
    // specific, so it overrides them.
    block(':root', themeDeclarations(DEFAULT_THEMES.light)),
    media('prefers-color-scheme: dark', block(':root', themeDeclarations(DEFAULT_THEMES.dark))),
    ...names.map(name => block(`:root[data-theme="${name}"]`, themeDeclarations(name))),
  ];
}

/**
 * The text of `tokens.css`: the scales and derived values on `:root`, one block per theme, the
 * reduced-motion override, the legacy aliases, then `primitives` (the hand-written styles of the
 * primitives, from `src/primitives.css`) verbatim.
 */
export function renderTokensCss(primitives: string): string {
  const aliases = Object.entries(LEGACY_ALIASES).map(([name, value]) => `--${name}: ${value};`);
  return `${[
    HEADER,
    block(':root', [...scaleDeclarations(), ...derivedDeclarations(), ...aliases]),
    ...themeBlocks(),
    reducedMotion(),
    primitives.trim(),
  ].join('\n')}\n`;
}

/**
 * Why `current` (the checked-in `tokens.css`) is not what the generator writes, or null when it
 * is. The message names the file, the rule and the fix.
 */
export function tokensCssDrift(current: string, primitives: string): string | null {
  if (current === renderTokensCss(primitives)) return null;
  return (
    `${TOKENS_CSS_PATH} does not match its generator (rule S9: tokens.css is generated from ` +
    `src/tokens/ and src/primitives.css, never edited by hand). Regenerate it with \`${TOKENS_CSS_COMMAND}\` ` +
    'and commit the result.'
  );
}
