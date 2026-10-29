import type {JSX} from 'react';
import {THEMES, type ThemeName} from '../tokens/index.js';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

/** The preview's colors, read from the theme's row of the token table. */
function previewColors(theme: ThemeName): StyleWithVars {
  const {colors} = THEMES[theme];
  return {
    '--swatch-bg': colors.bg,
    '--swatch-line': colors.line,
    '--swatch-text': colors.text,
    '--swatch-muted': colors.muted,
    '--swatch-accent': colors.accent,
    '--swatch-panel': colors['panel-2'],
  };
}

/**
 * A theme to choose: a small preview drawn in that theme's colors, whatever theme the page is in,
 * and its name. Pressed while `selected`.
 */
export function ThemeSwatch(props: {
  readonly theme: ThemeName;
  readonly label: string;
  readonly selected: boolean;
  readonly press: () => void;
}): JSX.Element {
  return (
    <button type="button" className="vs-swatch" aria-pressed={props.selected} onClick={props.press}>
      <span className="vs-swatch-preview" style={previewColors(props.theme)} aria-hidden="true">
        <span className="vs-swatch-row">
          <span className="vs-swatch-accent" />
          <span className="vs-swatch-bar" data-tone="text" data-width="wide" />
        </span>
        <span className="vs-swatch-bar" data-tone="muted" data-width="most" />
        <span className="vs-swatch-bar" data-tone="panel" data-width="full" />
        <span className="vs-swatch-bar" data-tone="muted" data-width="half" />
      </span>
      <Text variant="item">{props.label}</Text>
    </button>
  );
}
