import {
  BoxRenderable,
  type CliRenderer,
  type KeyEvent,
  TextareaRenderable,
  TextRenderable,
} from '@opentui/core';
import {suggestSlashCommands} from '../commands.js';
import type {ChatMenuRow, SessionState} from '../session-model.js';
import {SPINNER_FRAMES, SPINNER_INTERVAL_MS} from './activity-bar.js';
import {applyPaneFocus, paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import {elapsedLabel} from './previews.js';
import {SuggestionMenu} from './suggestion-menu.js';
import type {Theme} from './theme.js';

const MIN_EDITOR_ROWS = 1;
const MAX_EDITOR_ROWS = 6;
/** Border rows the message box adds around the editor it holds. */
const BOX_CHROME = 2;
/** The box's own rows plus the hint row above it. */
const COMPOSER_CHROME = BOX_CHROME + 1;
const EDITOR_HORIZONTAL_CHROME = 4;
/** Rows the menu shows at once before it scrolls its selection into view. */
const MAX_MENU_ROWS = 10;
const MENU_CHROME = 2;
const COMPOSER_TITLE = 'Message';
/**
 * Composer label while a question is in flight: spinner plus wait time. It is
 * unpadded because the pane frame owns the padding and the focus gutter, so
 * the wait and the focus marker compose instead of overwriting each other.
 */
export function pendingComposerLabel(frame: number, elapsedMs: number): string {
  const spinner = SPINNER_FRAMES[frame % SPINNER_FRAMES.length] ?? SPINNER_FRAMES[0];
  return `${COMPOSER_TITLE} · ${spinner} ${elapsedLabel(elapsedMs)}`;
}

class ChatTextareaRenderable extends TextareaRenderable {
  override handleKeyPress(key: KeyEvent): boolean {
    if (key.name === 'return' || key.name === 'kpenter' || key.name === 'linefeed') {
      if (key.shift) return this.newLine();
      if (!key.ctrl && !key.meta && !key.super && !key.hyper) return this.submit();
    }
    return super.handleKeyPress(key);
  }
}

/**
 * Draft shared by the docked and modal presentations of experiment chat. The
 * typed-command suggestion menu lives here too, alongside the text it is
 * derived from, so switching which presentation is on screen never resets or
 * duplicates the highlighted suggestion (see the multi-parent note on
 * `ChatComposerView` below).
 */
export interface ChatDraft {
  value: string;
  readonly suggestions: SuggestionMenu;
}

export function createChatDraft(): ChatDraft {
  return {value: '', suggestions: new SuggestionMenu()};
}

/**
 * The single experiment-chat composer used by every chat presentation.
 *
 * OpenTUI renderables cannot have two parents, so the dock and modal each own
 * an instance while sharing the draft above. Visibility transitions copy the
 * authoritative draft into the newly visible editor. This keeps resizing from
 * losing a partially written question without coupling either view to the
 * other's render tree.
 */
export class ChatComposerView {
  readonly output: BoxRenderable;
  /**
   * The composer's inline menu: chat commands as they are typed, and the row
   * selection `/model` and `/resume` open. It is a sibling of `output` rather
   * than a screen-level dialog, mounted by whichever chat surface owns this
   * composer, so the list rises out of the box it belongs to. This mirrors the
   * command input's suggestion list on the other side of the screen.
   */
  readonly menu: BoxRenderable;
  readonly #box: BoxRenderable;
  readonly #editor: ChatTextareaRenderable;
  readonly #hint: TextRenderable;
  readonly #menuList: TextRenderable;
  #availableWidth = 1;
  #theme: Theme;
  #renderedMenu: string | null = null;
  #lastState: SessionState | null = null;
  /** Whether the agent still owes an answer, which the title says. */
  #pending = false;
  #onScreen = false;
  #pendingSince = 0;
  #frame = 0;
  #timer: ReturnType<typeof setInterval> | null = null;

  constructor(
    renderer: CliRenderer,
    private readonly draft: ChatDraft,
    private readonly onSubmit: (value: string) => void,
    theme: Theme,
    id: string,
    private readonly onFocusRequest: () => void = () => {},
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: `${id}-composer`,
      width: '100%',
      height: COMPOSER_CHROME + MIN_EDITOR_ROWS,
      flexDirection: 'column',
      flexShrink: 0,
    });
    this.#box = new BoxRenderable(renderer, {
      id: `${id}-composer-box`,
      width: '100%',
      height: MIN_EDITOR_ROWS + BOX_CHROME,
      border: true,
      borderStyle: paneBorderStyle(false),
      borderColor: paneBorderColor(theme, false),
      title: paneTitle(COMPOSER_TITLE, false),
      paddingLeft: 1,
      paddingRight: 1,
      onMouseUp: this.onFocusRequest,
    });
    this.#editor = new ChatTextareaRenderable(renderer, {
      id: `${id}-composer-editor`,
      width: '100%',
      height: MIN_EDITOR_ROWS,
      initialValue: draft.value,
      placeholder: 'Ask about this experiment',
      wrapMode: 'word',
      textColor: theme.textStrong,
      focusedTextColor: theme.textStrong,
      onMouseUp: this.onFocusRequest,
      onContentChange: () => {
        this.draft.value = this.#editor.plainText;
        this.#resize();
        this.#syncSuggestions();
        // Typing does not go through the controller, so the command
        // suggestions have to follow the draft rather than a state change.
        if (this.#lastState !== null) this.renderMenu(this.#lastState);
      },
      onSubmit: () => this.#submit(),
    });
    this.#hint = new TextRenderable(renderer, {
      id: `${id}-composer-hint`,
      width: '100%',
      height: 1,
      wrapMode: 'none',
      truncate: true,
      fg: theme.textSubtle,
      content: 'Enter: send · Shift+Enter: newline',
    });
    this.menu = new BoxRenderable(renderer, {
      id: `${id}-composer-menu`,
      position: 'absolute',
      // Anchored on the message box, not above the whole composer: the hint
      // row sits over the box, and a menu cleared of it would float with that
      // row showing through the gap. The command bar's list sits on its own
      // box the same way. Kept in step with the editor's height below.
      bottom: MIN_EDITOR_ROWS + BOX_CHROME,
      left: 0,
      width: '100%',
      height: 3,
      visible: false,
      zIndex: 5,
      border: true,
      // Square with an outer fill, the overlay exception to the fill rule
      // (tui-conventions.md): this menu floats over the composer, so the fill
      // has to reach the border ring or the text under it shows through there,
      // and a fill that reaches the ring can only be honest under a square
      // corner.
      borderStyle: 'single',
      borderColor: theme.border,
      backgroundColor: theme.canvas,
      paddingLeft: 1,
      paddingRight: 1,
    });
    this.#menuList = new TextRenderable(renderer, {
      id: `${id}-composer-menu-list`,
      width: '100%',
      height: 1,
      fg: theme.textPrimary,
      wrapMode: 'none',
      truncate: true,
      content: '',
    });
    this.menu.add(this.#menuList);
    this.#box.add(this.#editor);
    // Hint first, then the box. The command column on the other side of the
    // landing view is ordered the same way, so both columns end on a bordered
    // input and the two boxes share a row.
    this.output.add(this.#hint);
    this.output.add(this.#box);
  }

  /**
   * Fills the inline menu. The controller-owned menu wins over the typed
   * command suggestions: once `/model` is submitted, the list is a selection
   * rather than a completion.
   */
  renderMenu(state: SessionState): void {
    this.#lastState = state;
    const lines = this.#menuLines(state);
    const fingerprint = lines === null ? null : lines.join('\n');
    if (this.#renderedMenu === fingerprint) return;
    this.#renderedMenu = fingerprint;
    this.menu.visible = lines !== null;
    if (lines === null) return;
    this.#menuList.content = lines.join('\n');
    this.#menuList.height = Math.max(1, lines.length);
    this.menu.height = lines.length + MENU_CHROME;
  }

  /** The menu's rows, or null when nothing should be on screen. */
  #menuLines(state: SessionState): string[] | null {
    const menu = state.chatMenu;
    if (menu !== null) {
      const rows = visibleRows(menu.rows, menu.selected);
      return [
        menu.title,
        ...rows.map(({row, index}) => menuRowText(row, index === menu.selected, menu.customModels)),
      ];
    }
    if (!this.draft.suggestions.visible) return null;
    return this.draft.suggestions.renderLines(this.draft.value.trim());
  }

  /**
   * Highlights, navigates, and Tab-completes the typed-command suggestions
   * the same way the command bar does. Callers gate these on the menu
   * actually showing suggestions (rather than the controller-owned
   * `ChatMenu`), since both share the one inline list.
   */
  navigateSuggestions(direction: 1 | -1): boolean {
    if (!this.draft.suggestions.navigate(direction)) return false;
    if (this.#lastState !== null) this.renderMenu(this.#lastState);
    return true;
  }

  completeSuggestion(): boolean {
    const value = this.draft.suggestions.complete(this.draft.value);
    if (value === null) return false;
    this.#editor.setText(value);
    this.#editor.cursorOffset = value.length;
    return true;
  }

  /** Recomputes the typed-command matches for the draft's current text. */
  #syncSuggestions(): void {
    this.draft.suggestions.setMatches(
      suggestSlashCommands(this.draft.value.trim(), {surface: 'chat'}),
    );
  }

  /** Makes this editor authoritative when its presentation becomes visible. */
  activate(availableWidth: number, focused: boolean, pending: boolean): void {
    this.#availableWidth = Math.max(1, availableWidth);
    if (this.#editor.plainText !== this.draft.value) {
      this.#editor.setText(this.draft.value);
      this.#syncSuggestions();
    }
    this.#pending = pending;
    this.#applyChrome();
    this.#hint.content = pending
      ? 'Awaiting the agent · Enter: queue follow-up'
      : focused
        ? 'Enter: send · Shift+Enter: newline'
        : 'Ctrl+W to type here';
    this.#resize();
  }

  /**
   * Tracks the question in flight and whether this composer is on screen.
   *
   * Callers drive this on every render, before their own visibility gate, the
   * way `ActivityBarView.render` syncs its timer whether or not the bar is
   * shown: `activate` runs only while visible, so a question that lands while
   * this surface is hidden would otherwise leave the interval running forever
   * and the elapsed epoch stuck at the wait it started.
   *
   * The epoch moves only when a question starts, not when the surface returns,
   * so hiding and reshowing mid-question keeps the wait the operator has
   * actually been watching.
   */
  syncPending(pending: boolean, onScreen: boolean): void {
    if (pending && !this.#pending) {
      this.#pendingSince = Date.now();
      this.#frame = 0;
    }
    this.#pending = pending;
    this.#onScreen = onScreen;
    this.#refreshTitle();
    this.#syncTimer();
  }

  /**
   * Releases the animation timer. Pending state is cleared with it, so a
   * composer that is activated again after a destroyed render tree starts its
   * next question from a fresh epoch rather than a dead one.
   */
  destroy(): void {
    this.#pending = false;
    this.#onScreen = false;
    this.#syncTimer();
  }

  #syncTimer(): void {
    if (!this.#pending || !this.#onScreen) {
      if (this.#timer !== null) clearInterval(this.#timer);
      this.#timer = null;
      return;
    }
    if (this.#timer !== null) return;
    this.#timer = setInterval(() => {
      this.#frame = (this.#frame + 1) % SPINNER_FRAMES.length;
      this.#refreshTitle();
    }, SPINNER_INTERVAL_MS);
  }

  #refreshTitle(): void {
    this.#applyChrome();
  }

  isEmpty(): boolean {
    return this.draft.value.trim() === '';
  }

  focus(): void {
    this.#editor.focus();
  }

  /**
   * The composer's frame, always the resting one.
   *
   * The focus treatment names the pane the keys are on, and this box is inside
   * that pane rather than being one. Wearing it here drew the marker and the
   * focused border twice, one nested in the other, which is the ambiguity the
   * treatment exists to remove. Which composer has the cursor is carried by the
   * cursor itself and by the hint line under the box, both non-colour channels.
   */
  #applyChrome(): void {
    const label = this.#pending
      ? pendingComposerLabel(this.#frame, Date.now() - this.#pendingSince)
      : COMPOSER_TITLE;
    applyPaneFocus(this.#box, this.#theme, label, false);
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.#applyChrome();
    this.#editor.textColor = theme.textStrong;
    this.#editor.focusedTextColor = theme.textStrong;
    this.#hint.fg = theme.textSubtle;
    this.menu.borderColor = theme.border;
    this.menu.backgroundColor = theme.canvas;
    this.#menuList.fg = theme.textPrimary;
  }

  #resize(): void {
    const contentWidth = Math.max(1, this.#availableWidth - EDITOR_HORIZONTAL_CHROME);
    const rows = Math.min(
      MAX_EDITOR_ROWS,
      Math.max(MIN_EDITOR_ROWS, wrappedRows(this.draft.value, contentWidth)),
    );
    this.#editor.height = rows;
    this.#box.height = rows + BOX_CHROME;
    this.output.height = rows + COMPOSER_CHROME;
    // The menu sits on top of the box, so it moves with it.
    this.menu.bottom = this.#box.height;
  }

  #submit(): void {
    const value = this.draft.value;
    if (!value.trim()) return;
    this.draft.value = '';
    this.#editor.clear();
    this.#resize();
    this.#syncSuggestions();
    this.onSubmit(value);
  }
}

/**
 * Keeps the highlighted row on screen without scrolling machinery: a window of
 * at most `MAX_MENU_ROWS` rows that always contains the selection.
 */
function visibleRows(
  rows: readonly ChatMenuRow[],
  selected: number,
): {row: ChatMenuRow; index: number}[] {
  const indexed = rows.map((row, index) => ({row, index}));
  if (indexed.length <= MAX_MENU_ROWS) return indexed;
  const start = Math.min(
    Math.max(0, selected - Math.floor(MAX_MENU_ROWS / 2)),
    indexed.length - MAX_MENU_ROWS,
  );
  return indexed.slice(start, start + MAX_MENU_ROWS);
}

function menuRowText(
  row: ChatMenuRow,
  selected: boolean,
  customModels: Record<string, string>,
): string {
  const marker = selected ? '›' : ' ';
  if (row.kind === 'header') return `  ${row.label}`;
  if (row.kind === 'note') return `  ${row.label}`;
  if (row.kind === 'thread') return `${marker} ${row.label} · ${row.detail}`;
  if (row.kind === 'model') return `${marker}   ${row.label}`;
  const typed = customModels[row.provider] ?? '';
  // A cursor bar marks the free-text entry as somewhere to type, the same
  // affordance the wizard's model step used.
  return `${marker}   ${typed === '' ? row.label : typed}${selected ? '▏' : ''}`;
}

function wrappedRows(value: string, width: number): number {
  if (value.length === 0) return 1;
  return value
    .split('\n')
    .reduce((rows, line) => rows + Math.max(1, Math.ceil([...line].length / width)), 0);
}
