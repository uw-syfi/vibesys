import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import {type CommandSection, fuzzyMatchCommands, type PaletteCommand} from '../commands.js';
import {activeCommandSurface} from '../palette-model.js';
import {chatPaneVisible, type SessionState} from '../session-model.js';
import type {Theme} from './theme.js';

const NAME_WIDTH = 14;

const HINT = '↑↓: select · Enter: run · Esc: close';

/**
 * Whole-row, whole-column geometry, the same reasoning `overlay.ts` uses: a
 * fractional edge rounds the hint row back onto the scroll viewport's last
 * row, which hides the final match.
 */
const WIDTH_SHARE = 0.7;
const LEFT_SHARE = 0.15;
const HEIGHT_SHARE = 0.6;
const TOP_SHARE = 0.18;
const COMMAND_SURFACE_ROWS = 5;

/** Two borders, the query row, the hint row, and one line of content worth opening for. */
const MIN_ROWS = 5;

function sectionLabel(section: CommandSection): string {
  return section.charAt(0).toUpperCase() + section.slice(1);
}

/**
 * A registry row, or the section header above the first row of a new
 * section. Headers are never selectable, so the palette's selection index
 * only ever counts rows.
 */
type PaletteRow = {kind: 'header'; label: string} | {kind: 'command'; command: PaletteCommand};

/**
 * `fuzzyMatchCommands` already returns commands grouped by section (registry
 * order groups them, and filtering preserves order), so a header only needs
 * to appear on the first row of each run of a section.
 */
function paletteRows(matches: readonly PaletteCommand[]): readonly PaletteRow[] {
  const rows: PaletteRow[] = [];
  let lastSection: CommandSection | null = null;
  for (const command of matches) {
    if (command.section !== lastSection) {
      rows.push({kind: 'header', label: sectionLabel(command.section)});
      lastSection = command.section;
    }
    rows.push({kind: 'command', command});
  }
  return rows;
}

/**
 * The interactive command palette: every command `fuzzyMatchCommands` offers
 * for the surface that had the keys when it opened, filtered as the operator
 * types, navigated with the arrows, and run (or pre-filled) with Enter. This
 * is the one `/help` surface (#800); it replaces the static overlay text that
 * used to answer `/help` with `helpText()`.
 */
export class PaletteView {
  readonly output: BoxRenderable;
  readonly #query: TextRenderable;
  readonly #scroll: ScrollBoxRenderable;
  readonly #hint: TextRenderable;
  #theme: Theme;
  #renderedQuery: string | null = null;
  #renderedSelected = -1;
  #renderedSurface: 'command' | 'chat' | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'palette',
      position: 'absolute',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      // Square with an outer fill, the overlay exception (tui/conventions.md).
      borderStyle: 'single',
      borderColor: theme.info,
      backgroundColor: theme.canvas,
      title: ' Commands ',
      // Above every other modal, including the theme picker (30, the next
      // highest): the palette is the one /help surface, so it has to sit
      // over the chat modal and the overlay it replaces.
      zIndex: 35,
      visible: false,
    });
    this.#query = new TextRenderable(renderer, {
      id: 'palette-query',
      width: '100%',
      height: 1,
      flexShrink: 0,
      wrapMode: 'none',
      truncate: true,
    });
    this.#scroll = new ScrollBoxRenderable(renderer, {
      id: 'palette-scroll',
      width: '100%',
      flexGrow: 1,
      stickyScroll: false,
      viewportCulling: true,
      verticalScrollbarOptions: {showArrows: false},
    });
    this.#hint = new TextRenderable(renderer, {
      id: 'palette-hint',
      content: HINT,
      fg: theme.textSubtle,
      width: '100%',
      height: 1,
      flexShrink: 0,
      wrapMode: 'none',
      truncate: true,
    });
    this.output.add(this.#query);
    this.output.add(this.#scroll);
    this.output.add(this.#hint);
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.output.borderColor = theme.info;
    this.output.backgroundColor = theme.canvas;
    this.#hint.fg = theme.textSubtle;
    this.#renderedQuery = null;
    this.#renderedSelected = -1;
    this.#renderedSurface = null;
  }

  render(state: SessionState): void {
    const palette = state.palette;
    if (palette === null) {
      this.output.visible = false;
      this.#renderedQuery = null;
      this.#renderedSelected = -1;
      this.#renderedSurface = null;
      return;
    }
    this.output.visible = true;
    this.#applyGeometry();
    const surface = activeCommandSurface(state);
    if (
      this.#renderedQuery === palette.query &&
      this.#renderedSelected === palette.selected &&
      this.#renderedSurface === surface
    ) {
      return;
    }
    this.#renderedQuery = palette.query;
    this.#renderedSelected = palette.selected;
    this.#renderedSurface = surface;
    this.#query.content = palette.query === '' ? '❭ Type to filter...' : `❭ ${palette.query}`;
    this.#query.fg = palette.query === '' ? this.#theme.textSubtle : this.#theme.textStrong;
    const matches = fuzzyMatchCommands(palette.query, {
      surface,
      chatDocked: chatPaneVisible(state),
    });
    this.#renderRows(matches, palette.selected);
  }

  #renderRows(matches: readonly PaletteCommand[], selected: number): void {
    this.#clear();
    if (matches.length === 0) {
      this.#scroll.add(
        new TextRenderable(this.renderer, {
          content: 'No matching commands',
          fg: this.#theme.textSubtle,
          width: '100%',
        }),
      );
      this.#scroll.scrollTo(0);
      return;
    }
    let commandIndex = 0;
    let rowIndex = 0;
    let selectedRow = 0;
    for (const row of paletteRows(matches)) {
      if (row.kind === 'header') {
        this.#scroll.add(
          new TextRenderable(this.renderer, {
            content: row.label,
            fg: this.#theme.textSubtle,
            width: '100%',
          }),
        );
        rowIndex += 1;
        continue;
      }
      const isSelected = commandIndex === selected;
      if (isSelected) selectedRow = rowIndex;
      commandIndex += 1;
      rowIndex += 1;
      this.#scroll.add(
        new TextRenderable(this.renderer, {
          content: this.#commandLine(row.command, isSelected),
          fg: isSelected ? this.#theme.textStrong : this.#theme.textPrimary,
          bg: isSelected ? this.#theme.selectedSurface : this.#theme.canvas,
          width: '100%',
        }),
      );
    }
    this.#followSelection(selectedRow, rowIndex);
  }

  /**
   * Keeps the highlighted row on screen as the arrows move it past either
   * edge of the viewport, the same clamp `experiment-log.ts` uses for its own
   * keyboard selection.
   */
  #followSelection(selected: number, total: number): void {
    const viewport = this.#viewportRows();
    const top = Math.min(this.#scroll.scrollTop, Math.max(0, total - viewport));
    if (selected < top) this.#scroll.scrollTo(selected);
    else if (selected >= top + viewport) this.#scroll.scrollTo(selected - viewport + 1);
    else this.#scroll.scrollTo(top);
  }

  #viewportRows(): number {
    const height = this.#scroll.height;
    if (typeof height === 'number' && height > 0) return height;
    // Before the first layout pass, estimate from the terminal.
    return Math.max(1, Math.round(this.renderer.terminalHeight * HEIGHT_SHARE) - MIN_ROWS);
  }

  #commandLine(command: PaletteCommand, selected: boolean): string {
    const marker = selected ? '›' : ' ';
    const keybinding = command.keybinding === undefined ? '' : `  [${command.keybinding}]`;
    return `${marker} ${command.name.padEnd(NAME_WIDTH)} ${command.description}${keybinding}`;
  }

  /** Whole-row, whole-column geometry, recomputed as the terminal resizes. */
  #applyGeometry(): void {
    const columns = this.renderer.terminalWidth;
    const rows = this.renderer.terminalHeight;
    this.output.width = Math.round(columns * WIDTH_SHARE);
    this.output.left = Math.round(columns * LEFT_SHARE);
    const top = Math.round(rows * TOP_SHARE);
    this.output.top = top;
    this.output.height = Math.max(
      MIN_ROWS,
      Math.min(Math.round(rows * HEIGHT_SHARE), rows - COMMAND_SURFACE_ROWS - top),
    );
  }

  #clear(): void {
    for (const child of [...this.#scroll.getChildren()]) {
      this.#scroll.remove(child);
      child.destroyRecursively();
    }
  }
}
