import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import type {SessionState} from '../session-model.js';
import {listThemes, type Theme, type ThemeName} from './theme.js';

const NAME_WIDTH = 20;

/**
 * The theme list as a keyboard selection rather than static text. The
 * highlighted row is the one Enter applies; the active theme is spelled out
 * so the two are never confused when they differ.
 */
export class ThemePickerView {
  readonly output: BoxRenderable;
  #theme: Theme;
  #renderedSelection: ThemeName | null = null;
  #renderedActive: ThemeName | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'theme-picker',
      width: '70%',
      height: '60%',
      position: 'absolute',
      left: '15%',
      top: '18%',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      // Square with an outer fill, the overlay exception (tui-conventions.md):
      // the fill is what makes this modal opaque over whatever it covers, ring
      // included, and a fill that reaches the ring needs a square corner.
      borderStyle: 'single',
      borderColor: theme.info,
      backgroundColor: theme.elevatedSurface,
      title: ' Themes ',
      visible: false,
      zIndex: 30,
    });
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.output.borderColor = theme.info;
    this.output.backgroundColor = theme.elevatedSurface;
    // Rows carry theme colors, so they are rebuilt on the next render.
    this.#renderedSelection = null;
    this.#renderedActive = null;
  }

  render(state: SessionState): void {
    const picker = state.themePicker;
    if (picker === null) {
      this.output.visible = false;
      this.#renderedSelection = null;
      this.#renderedActive = null;
      return;
    }
    this.output.visible = true;
    if (this.#renderedSelection === picker.selected && this.#renderedActive === state.themeName) {
      return;
    }
    this.#renderedSelection = picker.selected;
    this.#renderedActive = state.themeName;
    this.#clear();
    for (const theme of listThemes()) {
      const selected = theme.name === picker.selected;
      const active = theme.name === state.themeName;
      // The marker and the word carry the state; color is never the only
      // signal, following the rule the themes were added under.
      const marker = selected ? '›' : ' ';
      const suffix = active ? ' · active' : '';
      this.output.add(
        new TextRenderable(this.renderer, {
          content: `${marker} ${theme.name.padEnd(NAME_WIDTH)} ${theme.label} (${theme.appearance})${suffix}`,
          fg: selected ? this.#theme.textStrong : this.#theme.textPrimary,
          bg: selected ? this.#theme.selectedSurface : this.#theme.elevatedSurface,
          width: '100%',
        }),
      );
    }
    this.output.add(
      new TextRenderable(this.renderer, {
        content: '',
        width: '100%',
      }),
    );
    this.output.add(
      new TextRenderable(this.renderer, {
        content:
          '↑↓: select · Enter: apply · Esc: close · applies to this session; pass --theme <name> at launch',
        fg: this.#theme.textSubtle,
        width: '100%',
      }),
    );
  }

  #clear(): void {
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
  }
}
