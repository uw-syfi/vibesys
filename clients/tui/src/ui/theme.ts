export const THEME_NAMES = [
  'dark',
  'light',
  'solarized-dark',
  'solarized-light',
  'catppuccin-mocha',
  'catppuccin-latte',
  'high-contrast-dark',
  'high-contrast-light',
] as const;

export type ThemeName = (typeof THEME_NAMES)[number];

export const DEFAULT_THEME_NAME: ThemeName = 'dark';

export type Appearance = 'light' | 'dark';

export type ConversationRole =
  | 'assistant'
  | 'user'
  | 'prompt'
  | 'analysis'
  | 'tool'
  | 'neutral'
  | 'success'
  | 'failure';

export const CONVERSATION_ROLES: readonly ConversationRole[] = [
  'assistant',
  'user',
  'prompt',
  'analysis',
  'tool',
  'neutral',
  'success',
  'failure',
];

export interface ConversationRoleColors {
  border: string;
  background: string;
  label: string;
  content: string;
}

export interface BandColors {
  foreground: string;
  background: string;
}

export interface MarkdownColors {
  default: string;
  heading: string;
  strong: string;
  em: string;
  code: string;
  codeBackground: string;
  link: string;
  blockquote: string;
  /** Tree-sitter `keyword` captures inside a fence with a shipped grammar. */
  keyword: string;
  /** Tree-sitter `string` captures inside a fence with a shipped grammar. */
  string: string;
  /** Tree-sitter `comment` captures inside a fence with a shipped grammar. */
  comment: string;
  /** Tree-sitter `number` captures inside a fence with a shipped grammar. */
  number: string;
}

export interface Theme {
  name: ThemeName;
  label: string;
  appearance: Appearance;

  /**
   * The contrast floor every token here clears, `textSubtle` excepted: it is
   * held to `SUBTLE_TEXT_MIN_CONTRAST` instead.
   *
   * Carried on the theme rather than kept in the spec because `buildTheme`
   * makes that guarantee against the canvas alone. A region that paints another
   * surface has to remake it there, and the alternative to reading the number
   * is restating it.
   */
  minContrast: number;

  /**
   * The one background the UI paints. Every pane, modal, the header, the error
   * banner, the composer and the frame behind them all fill with this, so a box
   * is told from the page by its border and not by a shade of its own.
   *
   * There is deliberately no second, raised shade (#574). On a cell grid a
   * "raised" surface is a flat fill one step off the canvas: at a step small
   * enough to keep text readable it is not seen as depth, and at a step large
   * enough to be seen it bleeds a rectangle around content it is not marking.
   *
   * Where the two used to disagree this took the pane's value rather than the
   * frame's, because the panes cover nearly all of the screen. That made the
   * frame the minority colour, and the strips of it left showing between and
   * below the panes read as stray bands of light rather than as a background.
   * The key-help line was the plainest case: it paints no background of its own
   * and so falls through to the frame, which drew a pale rule across the bottom
   * of every dark theme.
   *
   * The two remaining fills name a state rather than a depth: `selectedSurface`
   * is what the cursor is on, and `surface` backs a code block, which has no
   * border to delimit it.
   */
  canvas: string;
  surface: string;
  selectedSurface: string;

  textPrimary: string;
  textMuted: string;
  textSubtle: string;
  textStrong: string;

  border: string;
  borderStrong: string;
  borderFocus: string;

  accent: string;
  info: string;

  success: string;
  warning: string;
  error: string;

  conversation: Record<ConversationRole, ConversationRoleColors>;
  toolCall: BandColors;
  toolResult: BandColors;
  markdown: MarkdownColors;
}

type Channels = readonly [number, number, number];

function parseHex(hex: string): Channels {
  const value = hex.startsWith('#') ? hex.slice(1) : hex;
  const full =
    value.length === 3
      ? value
          .split('')
          .map(character => character + character)
          .join('')
      : value;
  const parsed = Number.parseInt(full, 16);
  if (full.length !== 6 || Number.isNaN(parsed)) {
    throw new Error(`Invalid hex color: ${hex}`);
  }
  return [(parsed >> 16) & 0xff, (parsed >> 8) & 0xff, parsed & 0xff];
}

function toHex(channels: Channels): string {
  const hex = channels
    .map(channel =>
      Math.round(Math.min(255, Math.max(0, channel)))
        .toString(16)
        .padStart(2, '0'),
    )
    .join('');
  return `#${hex}`;
}

function channelLuminance(channel: number): number {
  const ratio = channel / 255;
  return ratio <= 0.03928 ? ratio / 12.92 : ((ratio + 0.055) / 1.055) ** 2.4;
}

export function relativeLuminance(hex: string): number {
  const [red, green, blue] = parseHex(hex);
  return (
    0.2126 * channelLuminance(red) +
    0.7152 * channelLuminance(green) +
    0.0722 * channelLuminance(blue)
  );
}

export function contrastRatio(left: string, right: string): number {
  const first = relativeLuminance(left);
  const second = relativeLuminance(right);
  const lighter = Math.max(first, second);
  const darker = Math.min(first, second);
  return (lighter + 0.05) / (darker + 0.05);
}

export function mix(from: string, to: string, amount: number): string {
  const start = parseHex(from);
  const end = parseHex(to);
  const clamped = Math.min(1, Math.max(0, amount));
  return toHex([
    start[0] + (end[0] - start[0]) * clamped,
    start[1] + (end[1] - start[1]) * clamped,
    start[2] + (end[2] - start[2]) * clamped,
  ]);
}

const CONTRAST_STEPS = 24;

export function ensureContrast(foreground: string, background: string, minRatio: number): string {
  if (contrastRatio(foreground, background) >= minRatio) return foreground;
  const target = relativeLuminance(background) > 0.5 ? '#000000' : '#ffffff';
  let candidate = foreground;
  for (let step = 1; step <= CONTRAST_STEPS; step += 1) {
    candidate = mix(foreground, target, step / CONTRAST_STEPS);
    if (contrastRatio(candidate, background) >= minRatio) return candidate;
  }
  return candidate;
}

/**
 * The dim a modal paints over everything behind it, as a colour and how far
 * each background cell is pulled toward it. Applied by the renderer as an
 * alpha blend over the finished frame, so it changes colour without moving a
 * single cell.
 */
export interface Scrim {
  /** Colour every background cell is pulled toward. */
  color: string;
  /** How far it is pulled: 0 leaves the frame alone, 1 replaces it. */
  strength: number;
}

/**
 * Contrast the scrim leaves between background text and what it sits on. WCAG's
 * large-text floor: the run behind the modal stays recognizable, and the
 * comfortable-reading floor each theme guarantees (4.5, or 7 on the
 * high-contrast pair) is left to the modal, the one surface the scrim does not
 * paint over.
 */
const SCRIM_RESIDUAL_CONTRAST = 3;

/** A scrim that erases the background is a screen, not a dim. */
const MAX_SCRIM_STRENGTH = 0.85;

const SCRIM_STEPS = 100;

/**
 * Pulls the background toward the theme's own canvas, so it fades into the
 * surface it already sits on instead of toward a blend the palette never uses.
 * That darkens a dark theme and washes out a light one without an appearance
 * branch, and it never invents a mid-tone a high-contrast palette excludes.
 *
 * The strength is solved per theme rather than fixed, because the themes do not
 * start from the same contrast: one blend deep enough to recede Solarized
 * Dark's 5.6:1 body text leaves High Contrast Dark's 21:1 fully readable.
 * Solving for a residual lands every theme at the same recessed legibility.
 */
export function scrim(theme: Theme): Scrim {
  const color = theme.canvas;
  for (let step = 1; step <= SCRIM_STEPS; step += 1) {
    const strength = (step / SCRIM_STEPS) * MAX_SCRIM_STRENGTH;
    const faded = mix(theme.textPrimary, color, strength);
    if (contrastRatio(faded, color) <= SCRIM_RESIDUAL_CONTRAST) return {color, strength};
  }
  return {color, strength: MAX_SCRIM_STRENGTH};
}

interface ThemeSpec {
  name: ThemeName;
  label: string;
  appearance: Appearance;
  canvas: string;
  surface: string;
  selectedSurface: string;
  textPrimary: string;
  textMuted: string;
  textSubtle: string;
  textStrong: string;
  border: string;
  borderStrong: string;
  borderFocus: string;
  accent: string;
  info: string;
  success: string;
  warning: string;
  error: string;
  roleAccents: Record<ConversationRole, string>;
  minContrast: number;
  cardTint: number;
  overrides?: {
    conversation?: Partial<Record<ConversationRole, Partial<ConversationRoleColors>>>;
    toolCall?: Partial<BandColors>;
    toolResult?: Partial<BandColors>;
    markdown?: Partial<MarkdownColors>;
  };
}

/** The lower floor `textSubtle` is held to: punctuation and rules, not words. */
export const SUBTLE_TEXT_MIN_CONTRAST = 3;

/**
 * How far a card's label is pulled toward the theme's strongest text before the
 * contrast floor is applied. The raw role accent reads as a border but is too
 * close to the card fill to head it: lifting it keeps the role's hue while
 * giving the label the presence a heading needs, in light themes as in dark.
 */
const LABEL_LIFT = 0.35;

function buildConversationRole(spec: ThemeSpec, role: ConversationRole): ConversationRoleColors {
  const accent = spec.roleAccents[role];
  // A low tint off the canvas: enough to group a card's lines together, not so
  // much that a screen of cards becomes a field of blocks.
  const background = mix(spec.canvas, accent, spec.cardTint);
  const derived: ConversationRoleColors = {
    border: accent,
    background,
    label: ensureContrast(mix(accent, spec.textStrong, LABEL_LIFT), background, spec.minContrast),
    content: ensureContrast(spec.textPrimary, background, spec.minContrast),
  };
  return {...derived, ...spec.overrides?.conversation?.[role]};
}

function buildToolBands(
  spec: ThemeSpec,
  conversation: Record<ConversationRole, ConversationRoleColors>,
): {toolCall: BandColors; toolResult: BandColors} {
  const tool = conversation.tool;
  return {
    toolCall: {
      background: tool.background,
      foreground: ensureContrast(spec.roleAccents.user, tool.background, spec.minContrast),
      ...spec.overrides?.toolCall,
    },
    toolResult: {
      background: tool.background,
      foreground: tool.content,
      ...spec.overrides?.toolResult,
    },
  };
}

function buildMarkdown(spec: ThemeSpec): MarkdownColors {
  const codeBackground = spec.surface;
  const derived: MarkdownColors = {
    default: ensureContrast(spec.textPrimary, spec.canvas, spec.minContrast),
    heading: ensureContrast(spec.accent, spec.canvas, spec.minContrast),
    strong: ensureContrast(spec.textStrong, spec.canvas, spec.minContrast),
    em: ensureContrast(spec.textMuted, spec.canvas, spec.minContrast),
    code: ensureContrast(spec.accent, codeBackground, spec.minContrast),
    codeBackground,
    link: ensureContrast(spec.info, spec.canvas, spec.minContrast),
    blockquote: ensureContrast(spec.textMuted, spec.canvas, spec.minContrast),
    // Checked against codeBackground, not canvas: these paint on a fenced
    // block's surface, not the canvas prose sits on.
    keyword: ensureContrast(spec.info, codeBackground, spec.minContrast),
    string: ensureContrast(spec.success, codeBackground, spec.minContrast),
    number: ensureContrast(spec.warning, codeBackground, spec.minContrast),
    comment: ensureContrast(spec.textMuted, codeBackground, spec.minContrast),
  };
  return {...derived, ...spec.overrides?.markdown};
}

function buildTheme(spec: ThemeSpec): Theme {
  const conversation = {} as Record<ConversationRole, ConversationRoleColors>;
  for (const role of CONVERSATION_ROLES) {
    conversation[role] = buildConversationRole(spec, role);
  }
  const {toolCall, toolResult} = buildToolBands(spec, conversation);
  return {
    name: spec.name,
    label: spec.label,
    appearance: spec.appearance,
    minContrast: spec.minContrast,
    canvas: spec.canvas,
    surface: spec.surface,
    selectedSurface: spec.selectedSurface,
    textPrimary: ensureContrast(spec.textPrimary, spec.canvas, spec.minContrast),
    textMuted: ensureContrast(spec.textMuted, spec.canvas, spec.minContrast),
    textSubtle: ensureContrast(spec.textSubtle, spec.canvas, SUBTLE_TEXT_MIN_CONTRAST),
    textStrong: ensureContrast(spec.textStrong, spec.canvas, spec.minContrast),
    border: spec.border,
    borderStrong: spec.borderStrong,
    borderFocus: spec.borderFocus,
    accent: ensureContrast(spec.accent, spec.canvas, spec.minContrast),
    info: ensureContrast(spec.info, spec.canvas, spec.minContrast),
    success: ensureContrast(spec.success, spec.canvas, spec.minContrast),
    warning: ensureContrast(spec.warning, spec.canvas, spec.minContrast),
    error: ensureContrast(spec.error, spec.canvas, spec.minContrast),
    conversation,
    toolCall,
    toolResult,
    markdown: buildMarkdown(spec),
  };
}

const DARK: ThemeSpec = {
  name: 'dark',
  label: 'Dark',
  appearance: 'dark',
  canvas: '#020617',
  surface: '#1e293b',
  // Distinct from the canvas: a selection painted in the canvas colour is not a
  // selection. Every other theme already differs here.
  selectedSurface: '#1e293b',
  textPrimary: '#e2e8f0',
  textMuted: '#94a3b8',
  textSubtle: '#64748b',
  textStrong: '#f8fafc',
  border: '#475569',
  borderStrong: '#334155',
  borderFocus: '#22d3ee',
  accent: '#22d3ee',
  info: '#38bdf8',
  success: '#22c55e',
  warning: '#facc15',
  error: '#f87171',
  minContrast: 4.5,
  cardTint: 0.14,
  roleAccents: {
    assistant: '#0891b2',
    user: '#2563eb',
    prompt: '#3b82f6',
    analysis: '#8b5cf6',
    tool: '#64748b',
    neutral: '#94a3b8',
    success: '#22c55e',
    failure: '#ef4444',
  },
  overrides: {
    markdown: {
      default: '#e2e8f0',
      heading: '#67e8f9',
      strong: '#f8fafc',
      em: '#cbd5e1',
      code: '#a5f3fc',
      codeBackground: '#1e293b',
      link: '#38bdf8',
      blockquote: '#94a3b8',
    },
  },
};

const LIGHT: ThemeSpec = {
  name: 'light',
  label: 'Light',
  appearance: 'light',
  canvas: '#ffffff',
  surface: '#f1f5f9',
  selectedSurface: '#e2e8f0',
  textPrimary: '#0f172a',
  textMuted: '#475569',
  textSubtle: '#64748b',
  textStrong: '#020617',
  border: '#b6c2d1',
  borderStrong: '#94a3b8',
  borderFocus: '#0e7490',
  accent: '#0e7490',
  info: '#0369a1',
  success: '#15803d',
  warning: '#b45309',
  error: '#b91c1c',
  minContrast: 4.5,
  cardTint: 0.1,
  roleAccents: {
    assistant: '#0e7490',
    user: '#1d4ed8',
    prompt: '#2563eb',
    analysis: '#6d28d9',
    tool: '#52525b',
    neutral: '#64748b',
    success: '#15803d',
    failure: '#b91c1c',
  },
};

const SOLARIZED_DARK: ThemeSpec = {
  name: 'solarized-dark',
  label: 'Solarized Dark',
  appearance: 'dark',
  canvas: '#001f27',
  surface: '#073642',
  selectedSurface: '#073642',
  textPrimary: '#93a1a1',
  textMuted: '#839496',
  textSubtle: '#657b83',
  textStrong: '#fdf6e3',
  border: '#586e75',
  borderStrong: '#657b83',
  borderFocus: '#268bd2',
  accent: '#2aa198',
  info: '#268bd2',
  success: '#859900',
  warning: '#b58900',
  error: '#dc322f',
  minContrast: 4.5,
  cardTint: 0.16,
  roleAccents: {
    assistant: '#2aa198',
    user: '#268bd2',
    prompt: '#6c71c4',
    analysis: '#d33682',
    tool: '#586e75',
    neutral: '#657b83',
    success: '#859900',
    failure: '#dc322f',
  },
};

const SOLARIZED_LIGHT: ThemeSpec = {
  name: 'solarized-light',
  label: 'Solarized Light',
  appearance: 'light',
  canvas: '#fffbf0',
  surface: '#eee8d5',
  selectedSurface: '#eee8d5',
  textPrimary: '#073642',
  textMuted: '#586e75',
  textSubtle: '#657b83',
  textStrong: '#002b36',
  border: '#93a1a1',
  borderStrong: '#657b83',
  borderFocus: '#268bd2',
  accent: '#2aa198',
  info: '#268bd2',
  success: '#859900',
  warning: '#b58900',
  error: '#dc322f',
  minContrast: 4.5,
  cardTint: 0.12,
  roleAccents: {
    assistant: '#2aa198',
    user: '#268bd2',
    prompt: '#6c71c4',
    analysis: '#d33682',
    tool: '#657b83',
    neutral: '#586e75',
    success: '#859900',
    failure: '#dc322f',
  },
};

const CATPPUCCIN_MOCHA: ThemeSpec = {
  name: 'catppuccin-mocha',
  label: 'Catppuccin Mocha',
  appearance: 'dark',
  canvas: '#11111b',
  surface: '#181825',
  selectedSurface: '#313244',
  textPrimary: '#cdd6f4',
  textMuted: '#a6adc8',
  textSubtle: '#7f849c',
  textStrong: '#f5e0dc',
  border: '#45475a',
  borderStrong: '#585b70',
  borderFocus: '#89b4fa',
  accent: '#94e2d5',
  info: '#89b4fa',
  success: '#a6e3a1',
  warning: '#f9e2af',
  error: '#f38ba8',
  minContrast: 4.5,
  cardTint: 0.16,
  roleAccents: {
    assistant: '#94e2d5',
    user: '#89b4fa',
    prompt: '#b4befe',
    analysis: '#cba6f7',
    tool: '#6c7086',
    neutral: '#9399b2',
    success: '#a6e3a1',
    failure: '#f38ba8',
  },
};

const CATPPUCCIN_LATTE: ThemeSpec = {
  name: 'catppuccin-latte',
  label: 'Catppuccin Latte',
  appearance: 'light',
  canvas: '#ffffff',
  surface: '#e6e9ef',
  selectedSurface: '#ccd0da',
  textPrimary: '#4c4f69',
  textMuted: '#6c6f85',
  textSubtle: '#8c8fa1',
  textStrong: '#1e1e2e',
  border: '#b2b7c4',
  borderStrong: '#9ca2b3',
  borderFocus: '#1e66f5',
  accent: '#179299',
  info: '#1e66f5',
  success: '#40a02b',
  warning: '#df8e1d',
  error: '#d20f39',
  minContrast: 4.5,
  cardTint: 0.12,
  roleAccents: {
    assistant: '#179299',
    user: '#1e66f5',
    prompt: '#7287fd',
    analysis: '#8839ef',
    tool: '#7c7f93',
    neutral: '#6c6f85',
    success: '#40a02b',
    failure: '#d20f39',
  },
};

const HIGH_CONTRAST_DARK: ThemeSpec = {
  name: 'high-contrast-dark',
  label: 'High Contrast Dark',
  appearance: 'dark',
  canvas: '#000000',
  surface: '#0a0a0a',
  selectedSurface: '#262626',
  textPrimary: '#ffffff',
  textMuted: '#e6e6e6',
  textSubtle: '#c7c7c7',
  textStrong: '#ffffff',
  border: '#d4d4d4',
  borderStrong: '#ffffff',
  borderFocus: '#ffff00',
  accent: '#00ffff',
  info: '#66d9ff',
  success: '#00ff7f',
  warning: '#ffd700',
  error: '#ff8080',
  minContrast: 7,
  cardTint: 0.1,
  roleAccents: {
    assistant: '#00ffff',
    user: '#7cc7ff',
    prompt: '#9ad1ff',
    analysis: '#e08cff',
    tool: '#d4d4d4',
    neutral: '#ffffff',
    success: '#00ff7f',
    failure: '#ff8080',
  },
};

const HIGH_CONTRAST_LIGHT: ThemeSpec = {
  name: 'high-contrast-light',
  label: 'High Contrast Light',
  appearance: 'light',
  canvas: '#ffffff',
  surface: '#f2f2f2',
  selectedSurface: '#d9d9d9',
  textPrimary: '#000000',
  textMuted: '#1a1a1a',
  textSubtle: '#3d3d3d',
  textStrong: '#000000',
  border: '#333333',
  borderStrong: '#000000',
  // Navy, not the brighter #0000cc this used to be. Against a white canvas
  // that blue was *quieter* than the resting #333333 border (11.22 against
  // 12.63), so focusing a pane made it recede. Focus must never be quieter
  // than rest; `focus.test.ts` pins that for every theme.
  borderFocus: '#000080',
  accent: '#006466',
  info: '#0033cc',
  success: '#006400',
  warning: '#7a4b00',
  error: '#b00000',
  minContrast: 7,
  cardTint: 0.08,
  roleAccents: {
    assistant: '#006466',
    user: '#0033cc',
    prompt: '#003bb3',
    analysis: '#6a0dad',
    tool: '#333333',
    neutral: '#000000',
    success: '#006400',
    failure: '#b00000',
  },
};

const SPECS: Record<ThemeName, ThemeSpec> = {
  dark: DARK,
  light: LIGHT,
  'solarized-dark': SOLARIZED_DARK,
  'solarized-light': SOLARIZED_LIGHT,
  'catppuccin-mocha': CATPPUCCIN_MOCHA,
  'catppuccin-latte': CATPPUCCIN_LATTE,
  'high-contrast-dark': HIGH_CONTRAST_DARK,
  'high-contrast-light': HIGH_CONTRAST_LIGHT,
};

const THEMES: Record<ThemeName, Theme> = Object.fromEntries(
  THEME_NAMES.map(name => [name, buildTheme(SPECS[name])]),
) as Record<ThemeName, Theme>;

export function isThemeName(value: string | undefined | null): value is ThemeName {
  return typeof value === 'string' && (THEME_NAMES as readonly string[]).includes(value);
}

export function resolveTheme(name: string | undefined | null): Theme {
  const theme = isThemeName(name) ? THEMES[name] : THEMES[DEFAULT_THEME_NAME];
  return theme;
}

export function listThemes(): Theme[] {
  return THEME_NAMES.map(name => THEMES[name]);
}
