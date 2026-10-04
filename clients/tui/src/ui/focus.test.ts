import {describe, expect, it} from 'bun:test';
import {contrastRatio, listThemes, resolveTheme, type Theme} from '../theme.js';
import {applyPaneFocus, paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';

const THEMES = listThemes().map(theme => [theme.name, theme] as const);

describe('focus channels', () => {
  it('reserves the marker cell so the label never moves', () => {
    expect(paneTitle('Transcript', true)).toBe(' ▸ Transcript   ');
    expect(paneTitle('Transcript', false)).toBe('   Transcript   ');
    // Whitespace is symmetric about the label: three cells each side, the
    // marker occupying one of the three on the left.
    for (const focused of [true, false]) {
      const title = paneTitle('Transcript', focused);
      const lead = title.indexOf('Transcript');
      const trail = title.length - lead - 'Transcript'.length;
      expect(trail).toBe(lead);
    }
    for (const label of ['Transcript', 'Experiments', 'Hypothesis H-08', 'Message']) {
      const focused = paneTitle(label, true);
      const resting = paneTitle(label, false);
      expect(focused.length).toBe(resting.length);
      expect(focused.indexOf(label)).toBe(resting.indexOf(label));
    }
  });

  it('keeps the same frame whether or not a pane holds focus', () => {
    // Focus does not change the frame. A heavier weight would mean square
    // corners, since Unicode has no heavy rounded corner, and that reads as a
    // different kind of object rather than a louder one. The marker carries
    // the non-colour signal instead.
    expect(paneBorderStyle(true)).toBe(paneBorderStyle(false));
    expect(paneBorderStyle(true)).toBe('rounded');
  });

  /**
   * The property that keeps focus legible when a palette cannot carry it.
   * Colour alone was the whole signal until this: `borderFocus` and `border`
   * sit within 1.5x of each other in five of the eight built-in themes, and
   * `high-contrast-light` had them inverted, so focusing a pane made it
   * quieter. Any regression that puts the signal back into colour alone fails
   * here rather than shipping.
   */
  it.each(
    THEMES,
  )('%s tells a focused pane from a resting one without using colour', (_name, theme: Theme) => {
    const focused = {
      title: paneTitle('Transcript', true),
      borderStyle: paneBorderStyle(true),
    };
    const resting = {
      title: paneTitle('Transcript', false),
      borderStyle: paneBorderStyle(false),
    };
    const nonColorChannels = [
      focused.title !== resting.title,
      focused.borderStyle !== resting.borderStyle,
    ].filter(Boolean);
    expect(nonColorChannels.length).toBeGreaterThanOrEqual(1);
    // The colour channel is allowed to be useless here, and in several
    // themes it is. It must never be worse than useless: a focused border
    // that is quieter than a resting one reads as focus being taken away.
    const focusColor = paneBorderColor(theme, true);
    const restingColor = paneBorderColor(theme, false);
    expect(contrastRatio(focusColor, theme.canvas)).toBeGreaterThanOrEqual(
      contrastRatio(restingColor, theme.canvas),
    );
  });

  it('applies every channel to one frame', () => {
    const theme = resolveTheme('dark');
    const box = {title: '', borderStyle: 'rounded', borderColor: ''};
    applyPaneFocus(box as never, theme, 'Agents', true);
    expect(box).toEqual({
      title: ' ▸ Agents   ',
      borderStyle: paneBorderStyle(true),
      borderColor: theme.borderFocus,
    });
    applyPaneFocus(box as never, theme, 'Agents', false);
    expect(box).toEqual({
      title: '   Agents   ',
      borderStyle: paneBorderStyle(false),
      borderColor: theme.border,
    });
  });
});
