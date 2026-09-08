import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import {focusedPane, type RightPane, type SessionState} from '../session-model.js';
import {applyPaneFocus} from './focus.js';
import type {Theme} from './theme.js';

type OverlayKind = NonNullable<SessionState['overlay']>['kind'];

const TITLE: Record<OverlayKind, string> = {
  detail: 'Command',
  help: 'Help',
  error: 'Error',
};

const HINT = 'Esc to close · PgUp/PgDn: scroll';

/**
 * The share of the screen the box takes. It is applied in whole rows and
 * columns rather than as a Yoga percentage: a fractional box edge rounds the
 * hint back onto the scroll viewport's last row, which hides the final line of
 * content.
 */
const WIDTH_SHARE = 0.7;
const LEFT_SHARE = 0.15;
const HEIGHT_SHARE = 0.6;
const TOP_SHARE = 0.18;

function borderFor(theme: Theme, kind: OverlayKind): string {
  if (kind === 'help') return theme.success;
  if (kind === 'error') return theme.error;
  return theme.info;
}

export class OverlayView {
  readonly output: BoxRenderable;
  readonly scrim: BoxRenderable;
  readonly #scroll: ScrollBoxRenderable;
  readonly #hint: TextRenderable;
  #theme: Theme;
  #renderedKind: OverlayKind | null = null;
  #renderedContent = '';
  #renderedPane: RightPane | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.scrim = new BoxRenderable(renderer, {
      id: 'overlay-scrim',
      position: 'absolute',
      width: '100%',
      height: '100%',
      backgroundColor: theme.canvas,
      opacity: 0.7,
      zIndex: 19,
      visible: false,
    });
    this.output = new BoxRenderable(renderer, {
      id: 'overlay',
      position: 'absolute',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: 'rounded',
      borderColor: theme.info,
      backgroundColor: theme.elevatedSurface,
      // Above the chat modal (20), below the theme picker (30): a command ack
      // submitted from the modal chat has to be visible over it.
      zIndex: 25,
    });
    this.#scroll = new ScrollBoxRenderable(renderer, {
      id: 'overlay-scroll',
      width: '100%',
      flexGrow: 1,
      stickyScroll: false,
      viewportCulling: true,
      verticalScrollbarOptions: {showArrows: false},
    });
    // The box owns its scroll viewport and its hint row for the whole life of
    // the view: only the scrolled content is rebuilt per overlay. The hint has
    // a reserved row of its own so it cannot overpaint the last content line.
    this.#hint = new TextRenderable(renderer, {
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

  /** Scrolled by Page Up/Page Down while the overlay is open. */
  scrollBy(delta: number): void {
    this.#scroll.scrollBy(delta, 'viewport');
  }

  renderScrim(visible: boolean): void {
    this.scrim.visible = visible;
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.output.backgroundColor = theme.elevatedSurface;
    this.scrim.backgroundColor = theme.canvas;
    this.output.borderColor = borderFor(theme, this.#renderedKind ?? 'detail');
    this.#hint.fg = theme.textSubtle;
    this.#renderedKind = null;
    this.#renderedContent = '';
    this.#renderedPane = null;
  }

  /**
   * `pane` is the visualization the terminal is too narrow to split, drawn here
   * because there is no column to put it in. It is the same surface as
   * `RightPaneView` and takes the same keys, so it wears that pane's title and
   * asks `focusedPane` the same question rather than appearing as a `Command`
   * box with none of the focus treatment on the one thing taking keystrokes.
   */
  render(state: SessionState, pane: RightPane | null = null): void {
    if (pane !== null) {
      this.#renderPane(state, pane);
      return;
    }
    this.#renderedPane = null;
    const overlay = state.overlay;
    if (overlay === null) {
      this.output.visible = false;
      // Scroll position belongs to one viewing of one overlay. Forgetting what
      // is on screen makes the next open rebuild the content, which puts the
      // viewport back at the top even when the same overlay is reopened.
      this.#renderedKind = null;
      this.#renderedContent = '';
      return;
    }
    this.output.visible = true;
    this.#applyGeometry();
    if (this.#renderedKind === overlay.kind && this.#renderedContent === overlay.content) return;
    this.#renderedKind = overlay.kind;
    this.#renderedContent = overlay.content;
    this.output.borderColor = borderFor(this.#theme, overlay.kind);
    this.output.title = ` ${TITLE[overlay.kind]} `;
    this.#clear();
    this.#body(
      overlay.content,
      overlay.kind === 'error' ? this.#theme.conversation.failure.content : this.#theme.textPrimary,
    );
  }

  #renderPane(state: SessionState, pane: RightPane): void {
    this.output.visible = true;
    this.#applyGeometry();
    // Outside the cache below: focus moves without the content changing.
    applyPaneFocus(this.output, this.#theme, pane.title, focusedPane(state) === 'performance');
    if (pane === this.#renderedPane) return;
    this.#renderedPane = pane;
    // The next ordinary overlay repaints its own title and border rather than
    // inheriting the pane's.
    this.#renderedKind = null;
    this.#renderedContent = '';
    this.#clear();
    if (pane.error !== null) this.#body(pane.error, this.#theme.conversation.failure.content);
    else if (pane.pending && pane.content === '') {
      this.#body('Loading...', this.#theme.textSubtle);
    } else this.#body(pane.content, this.#theme.textPrimary);
  }

  /** One wrapped block in the scroll viewport, rebuilt per overlay or pane. */
  #body(content: string, fg: string): void {
    this.#scroll.add(
      new TextRenderable(this.renderer, {
        content,
        fg,
        width: '100%',
        flexShrink: 0,
        wrapMode: 'word',
      }),
    );
    this.#scroll.scrollTo(0);
  }

  /** Whole-row, whole-column geometry, recomputed as the terminal resizes. */
  #applyGeometry(): void {
    const columns = this.renderer.terminalWidth;
    const rows = this.renderer.terminalHeight;
    this.output.width = Math.round(columns * WIDTH_SHARE);
    this.output.left = Math.round(columns * LEFT_SHARE);
    this.output.height = Math.round(rows * HEIGHT_SHARE);
    this.output.top = Math.round(rows * TOP_SHARE);
  }

  #clear(): void {
    for (const child of [...this.#scroll.getChildren()]) {
      this.#scroll.remove(child);
      child.destroyRecursively();
    }
  }
}
