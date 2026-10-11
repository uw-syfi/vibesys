import {
  BoxRenderable,
  CliRenderEvents,
  type CliRenderer,
  ScrollBoxRenderable,
  TextRenderable,
} from '@opentui/core';
import type {SessionState} from '../session-model.js';
import {listThemes, type Theme, type ThemeName} from '../theme.js';
import {fitModalRows, type ModalViewport} from './modal-geometry.js';

const NAME_WIDTH = 20;
const HEIGHT_SHARE = 0.6;
const TOP_SHARE = 0.18;
const HINT =
  '↑↓: select · Enter: apply · Esc: close · applies to this session; pass --theme <name> at launch';
const PICKER_CHROME_ROWS = 3;
/** Two borders, the fixed hint, and one selectable theme row. */
const MIN_ROWS = PICKER_CHROME_ROWS + 1;

/**
 * The theme list as a keyboard selection rather than static text. The
 * highlighted row is the one Enter applies; the active theme is spelled out
 * so the two are never confused when they differ.
 */
export class ThemePickerView {
  readonly output: BoxRenderable;
  readonly #scroll: ScrollBoxRenderable;
  readonly #hint: TextRenderable;
  #theme: Theme;
  #renderedSelection: ThemeName | null = null;
  #renderedActive: ThemeName | null = null;
  #renderedViewportRows: number | null = null;
  #pendingFollow: {selected: number; total: number; viewport: number} | null = null;
  #followScheduled = false;
  readonly #followAfterLayout = (): void => {
    this.#followScheduled = false;
    const pending = this.#pendingFollow;
    this.#pendingFollow = null;
    if (!this.output.visible || pending === null) return;
    this.#followSelection(pending.selected, pending.total, pending.viewport);
    this.renderer.requestRender();
  };

  constructor(
    private readonly renderer: CliRenderer,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'theme-picker',
      width: '70%',
      position: 'absolute',
      left: '15%',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      // Square with an outer fill, the overlay exception (tui/conventions.md):
      // the fill is what makes this modal opaque over whatever it covers, ring
      // included, and a fill that reaches the ring needs a square corner.
      borderStyle: 'single',
      borderColor: theme.info,
      backgroundColor: theme.canvas,
      title: ' Themes ',
      visible: false,
      zIndex: 30,
    });
    this.#scroll = new ScrollBoxRenderable(renderer, {
      id: 'theme-picker-scroll',
      width: '100%',
      flexGrow: 1,
      stickyScroll: false,
      viewportCulling: true,
      verticalScrollbarOptions: {showArrows: false},
    });
    this.#hint = new TextRenderable(renderer, {
      id: 'theme-picker-hint',
      content: HINT,
      fg: theme.textSubtle,
      width: '100%',
      height: 1,
      flexShrink: 0,
      wrapMode: 'none',
      truncate: true,
    });
    this.output.add(this.#scroll);
    this.output.add(this.#hint);
  }

  /** Cancels post-layout work before the root destroys this view's renderables. */
  destroy(): void {
    this.#pendingFollow = null;
    if (!this.#followScheduled) return;
    this.renderer.off(CliRenderEvents.FRAME, this.#followAfterLayout);
    this.#followScheduled = false;
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.output.borderColor = theme.info;
    this.output.backgroundColor = theme.canvas;
    this.#hint.fg = theme.textSubtle;
    // Rows carry theme colors, so they are rebuilt on the next render.
    this.#renderedSelection = null;
    this.#renderedActive = null;
  }

  render(state: SessionState, viewport: ModalViewport): void {
    const picker = state.themePicker;
    if (picker === null) {
      this.output.visible = false;
      this.#renderedSelection = null;
      this.#renderedActive = null;
      return;
    }
    this.output.visible = true;
    const rows = fitModalRows(
      viewport,
      Math.round(viewport.height * TOP_SHARE),
      Math.round(viewport.height * HEIGHT_SHARE),
      MIN_ROWS,
    );
    this.output.top = rows.top;
    this.output.height = rows.height;
    const viewportRows = Math.max(1, rows.height - PICKER_CHROME_ROWS);
    const geometryChanged = viewportRows !== this.#renderedViewportRows;
    this.#renderedViewportRows = viewportRows;
    const themes = listThemes();
    const selectedIndex = themes.findIndex(theme => theme.name === picker.selected);
    if (this.#renderedSelection === picker.selected && this.#renderedActive === state.themeName) {
      this.#followSelection(selectedIndex, themes.length, viewportRows);
      if (geometryChanged)
        this.#followSelectionAfterLayout(selectedIndex, themes.length, viewportRows);
      return;
    }
    this.#renderedSelection = picker.selected;
    this.#renderedActive = state.themeName;
    this.#clear();
    for (const theme of themes) {
      const selected = theme.name === picker.selected;
      const active = theme.name === state.themeName;
      // The marker and the word carry the state; color is never the only
      // signal, following the rule the themes were added under.
      const marker = selected ? '›' : ' ';
      const suffix = active ? ' · active' : '';
      this.#scroll.add(
        new TextRenderable(this.renderer, {
          content: `${marker} ${theme.name.padEnd(NAME_WIDTH)} ${theme.label} (${theme.appearance})${suffix}`,
          fg: selected ? this.#theme.textStrong : this.#theme.textPrimary,
          bg: selected ? this.#theme.selectedSurface : this.#theme.canvas,
          width: '100%',
          height: 1,
          flexShrink: 0,
          wrapMode: 'none',
          truncate: true,
        }),
      );
    }
    this.#followSelection(selectedIndex, themes.length, viewportRows);
    this.#followSelectionAfterLayout(selectedIndex, themes.length, viewportRows);
  }

  #followSelection(selected: number, total: number, viewport: number): void {
    const top = Math.min(this.#scroll.scrollTop, Math.max(0, total - viewport));
    if (selected < top) this.#scroll.scrollTo(selected);
    else if (selected >= top + viewport) this.#scroll.scrollTo(selected - viewport + 1);
    else this.#scroll.scrollTo(top);
  }

  #followSelectionAfterLayout(selected: number, total: number, viewport: number): void {
    this.#pendingFollow = {selected, total, viewport};
    if (this.#followScheduled) return;
    this.#followScheduled = true;
    this.renderer.once(CliRenderEvents.FRAME, this.#followAfterLayout);
  }

  #clear(): void {
    for (const child of [...this.#scroll.getChildren()]) {
      this.#scroll.remove(child);
      child.destroyRecursively();
    }
  }
}
