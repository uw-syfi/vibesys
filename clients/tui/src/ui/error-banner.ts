import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import type {ErrorBannerState, SessionState} from '../session-model.js';
import type {Theme} from './theme.js';

/** Enough rows to read a useful diagnostic without pushing the run off screen. */
const ERROR_HEIGHT = 10;

function context(banner: ErrorBannerState): string {
  return [banner.agentKind, banner.roundLabel]
    .filter((item): item is string => item !== null)
    .join(' · ');
}

/**
 * A single, always-rooted, fixed-height error surface. It does not take input
 * focus. Its footer owns explicit pointer dismissal, while global keybindings
 * own Escape and Ctrl+PgUp/Ctrl+PgDn.
 */
export class ErrorBannerView {
  readonly output: BoxRenderable;
  readonly #scroll: ScrollBoxRenderable;
  #theme: Theme;
  #rendered: ErrorBannerState | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    theme: Theme,
    private readonly onDismiss: () => void,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'error-banner',
      width: '100%',
      height: ERROR_HEIGHT,
      flexShrink: 0,
      flexDirection: 'column',
      border: true,
      borderStyle: 'rounded',
      borderColor: theme.error,
      paddingLeft: 1,
      paddingRight: 1,
      // No fill: the frame is rounded, and a box may have one or the other,
      // never both (tui-conventions.md). The banner is a flex child of `root`
      // holding its own rows, so nothing shows through where the fill was, and
      // what says "error" is the border colour `render` picks from the
      // severity plus the title, not a tint a high-contrast theme flattens
      // into the canvas anyway.
      visible: false,
    });
    this.#scroll = new ScrollBoxRenderable(renderer, {
      id: 'error-banner-scroll',
      width: '100%',
      flexGrow: 1,
      stickyScroll: false,
      viewportCulling: true,
      verticalScrollbarOptions: {showArrows: true},
    });
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    // `#rendered = null` makes the next `render` repaint the border from the
    // new theme, which is the only colour this box owns.
    this.#rendered = null;
  }

  scrollBy(delta: number): void {
    this.#scroll.scrollBy(delta, 'viewport');
  }

  render(state: SessionState): void {
    const banner = state.errorBanner;
    if (banner === null) {
      this.output.visible = false;
      this.#rendered = null;
      return;
    }
    this.output.visible = true;
    if (banner === this.#rendered) return;
    this.#rendered = banner;
    this.#clear();
    this.output.borderColor = banner.severity === 'fatal' ? this.#theme.error : this.#theme.warning;
    this.output.height = ERROR_HEIGHT;
    this.#renderContent(banner);
  }

  #renderContent(banner: ErrorBannerState): void {
    const where = context(banner);
    const count = banner.count > 1 ? ` · ${banner.count} reports` : '';
    this.output.title = ` ${banner.title}${where ? ` · ${where}` : ''}${count} `;
    this.output.add(this.#scroll);
    this.#scroll.add(
      new TextRenderable(this.renderer, {
        content: banner.message,
        fg:
          banner.severity === 'fatal'
            ? this.#theme.conversation.failure.content
            : this.#theme.warning,
        width: '100%',
        wrapMode: 'word',
      }),
    );
    if (banner.detail !== null) {
      this.#scroll.add(
        new TextRenderable(this.renderer, {
          content: `Detail: ${banner.detail}`,
          fg: this.#theme.textPrimary,
          width: '100%',
          wrapMode: 'word',
        }),
      );
    }
    if (banner.hint !== null) {
      this.#scroll.add(
        new TextRenderable(this.renderer, {
          content: `Hint: ${banner.hint}`,
          fg: this.#theme.warning,
          width: '100%',
          wrapMode: 'word',
        }),
      );
    }
    this.output.add(
      new TextRenderable(this.renderer, {
        content: '[× Dismiss] · Esc: dismiss · Ctrl+PgUp/PgDn: scroll',
        fg: this.#theme.textSubtle,
        width: '100%',
        height: 1,
        wrapMode: 'none',
        truncate: true,
        onMouseUp: this.onDismiss,
      }),
    );
    this.#scroll.scrollTo(0);
  }

  #clear(): void {
    this.output.title = '';
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      if (child !== this.#scroll) child.destroyRecursively();
    }
    for (const child of [...this.#scroll.getChildren()]) {
      this.#scroll.remove(child);
      child.destroyRecursively();
    }
  }
}
