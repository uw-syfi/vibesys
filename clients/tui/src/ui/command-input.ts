import {
  BoxRenderable,
  type CliRenderer,
  InputRenderable,
  InputRenderableEvents,
  SyntaxStyle,
  TextRenderable,
} from '@opentui/core';
import {type CommandContext, slashCommandRange, suggestSlashCommands} from '../commands.js';
import type {SessionState} from '../session-model.js';
import {paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import {SuggestionMenu} from './suggestion-menu.js';
import type {Theme} from './theme.js';

export interface CommandInputPanel {
  /** The hint row and the bordered box together: the unit a host pane mounts. */
  output: BoxRenderable;
  suggestions: BoxRenderable;
  /** Narrows the completions to the commands the current view offers. */
  setCommandContext(context: CommandContext): void;
  completeSuggestion(): boolean;
  navigateSuggestions(direction: 1 | -1): boolean;
  /** True when nothing is typed, so Enter belongs to whatever pane is behind. */
  isEmpty(): boolean;
  focus(): void;
  /** Reflects `state.inputError` onto the hint row and the box border. */
  render(state: SessionState): void;
  applyTheme(theme: Theme): void;
  destroy(): void;
}

const COMMAND_TITLE = 'Command';

/** Key hints shown on the reserved row above the box while no input error stands. */
const RESTING_HINT = 'Enter: run · Tab: complete';

/** The bordered box's own rows: top border, the input line, bottom border. */
const BOX_CHROME = 3;

/**
 * The box's own rows plus the hint row above it, mirroring `chat-composer.ts`'s
 * `COMPOSER_CHROME`. The row is reserved rather than inserted on demand: per
 * `tui-conventions.md`, a row that appears and disappears resizes everything
 * under it, so it is always present and only its content and colour change.
 */
const COMMAND_CHROME = BOX_CHROME + 1;

function commandSyntaxStyle(theme: Theme): SyntaxStyle {
  return SyntaxStyle.fromStyles({'slash-command': {fg: theme.accent, bold: true}});
}

export function createCommandInputPanel(
  renderer: CliRenderer,
  onSubmit: (value: string) => void,
  theme: Theme,
  /** Called when the box is clicked, so the pane focus follows the cursor. */
  onFocusRequest: () => void = () => {},
  /**
   * Called on every keystroke (typed or deleted). A stale input error names a
   * typo in text the operator is already retyping, so it clears as soon as
   * they touch the box again rather than waiting for them to notice and
   * dismiss it.
   */
  onChange: () => void = () => {},
): CommandInputPanel {
  let currentTheme = theme;
  /** The last message `render` painted, or null at rest. Skips redundant repaints. */
  let lastMessage: string | null = null;
  const output = new BoxRenderable(renderer, {
    id: 'command-input-panel',
    width: '100%',
    height: COMMAND_CHROME,
    flexDirection: 'column',
    flexShrink: 0,
  });
  const hint = new TextRenderable(renderer, {
    id: 'command-input-hint',
    width: '100%',
    height: 1,
    wrapMode: 'none',
    truncate: true,
    fg: theme.textSubtle,
    content: RESTING_HINT,
  });
  const box = new BoxRenderable(renderer, {
    id: 'command-input-box',
    height: BOX_CHROME,
    width: '100%',
    border: true,
    // The focus treatment names the one pane the navigation keys are on. This
    // box is drawn inside that pane rather than being one, so it never wears
    // the treatment: resting frame, resting colour, and the gutter cell where a
    // pane would put its marker. A second lit border made the marked pane
    // ambiguous, which is the whole complaint behind #433. It still takes the
    // title from `focus.ts` so its label sits at the same column as the pane
    // holding it, and as the chat's `Message` box across the landing view.
    // An input error is reinforcement, not the focus treatment: it turns this
    // border `theme.error` (see `render` below), a colour distinct from
    // `borderFocus`, while the border stays unfocused and the marker gutter
    // stays blank.
    borderStyle: paneBorderStyle(false),
    borderColor: paneBorderColor(theme, false),
    title: paneTitle(COMMAND_TITLE, false),
    paddingLeft: 1,
    paddingRight: 1,
    onMouseUp: onFocusRequest,
  });
  let syntaxStyle = commandSyntaxStyle(theme);
  let commandStyleId = syntaxStyle.getStyleId('slash-command');
  const input = new InputRenderable(renderer, {
    id: 'command-input',
    width: '100%',
    placeholder: 'Type /help for commands',
    textColor: theme.textStrong,
    focusedTextColor: theme.textStrong,
    syntaxStyle,
    onMouseUp: onFocusRequest,
  });
  const suggestions = new BoxRenderable(renderer, {
    id: 'command-input-suggestions',
    position: 'absolute',
    // Flush on the box, not the whole panel: like the chat composer's own
    // menu, a popup cleared of the hint row above would leave that row
    // floating in the gap while the list is open, so it sits on the box and
    // covers the hint instead (see chat-composer.ts's `menu` for the same
    // reasoning on the other side of the landing view).
    bottom: BOX_CHROME,
    left: 0,
    width: '100%',
    height: 3,
    visible: false,
    zIndex: 5,
    border: true,
    // Square with an outer fill, the overlay exception (tui-conventions.md):
    // this popup floats over the panes above the command column, so its fill
    // has to reach the border ring to stop them showing through, and that is
    // only honest under a square corner.
    borderStyle: 'single',
    borderColor: theme.border,
    backgroundColor: theme.selectedSurface,
    paddingLeft: 1,
    paddingRight: 1,
  });
  const suggestionList = new TextRenderable(renderer, {
    id: 'command-input-suggestion-list',
    width: '100%',
    height: 1,
    fg: theme.textMuted,
    wrapMode: 'none',
    truncate: true,
    content: '',
  });
  suggestions.add(suggestionList);
  const menu = new SuggestionMenu();
  let context: CommandContext = {};

  const updateDecorations = (value: string): void => {
    input.clearAllHighlights();
    const range = slashCommandRange(value);
    if (range !== null && commandStyleId !== null) {
      input.addHighlightByCharRange({...range, styleId: commandStyleId});
    }

    menu.setMatches(suggestSlashCommands(value, {surface: 'command', ...context}));
    suggestions.visible = menu.visible;
    suggestions.height = menu.matches.length + 2;
    suggestionList.height = Math.max(1, menu.matches.length);
    suggestionList.content = menu.renderLines(value).join('\n');
  };
  const handleInput = (value: string): void => {
    updateDecorations(value);
    onChange();
  };
  const submit = (value: string): void => {
    input.value = '';
    // Enter on an empty (or whitespace-only) box belongs to whatever pane is
    // behind it, not to command parsing; see isEmpty() above.
    if (value.trim() === '') return;
    onSubmit(value);
  };
  input.on(InputRenderableEvents.INPUT, handleInput);
  input.on(InputRenderableEvents.ENTER, submit);
  box.add(input);
  // Hint first, then the box, the same order as the chat composer on the
  // other side of the landing view, so both columns end on a bordered input
  // and the two boxes share a row (app.test.ts pins this).
  output.add(hint);
  output.add(box);
  return {
    output,
    suggestions,
    setCommandContext(next: CommandContext): void {
      if (next.chatDocked === context.chatDocked) return;
      context = next;
      updateDecorations(input.value);
    },
    completeSuggestion(): boolean {
      const value = menu.complete(input.value);
      if (value === null) return false;
      input.value = value;
      return true;
    },
    navigateSuggestions(direction: 1 | -1): boolean {
      if (!menu.navigate(direction)) return false;
      suggestionList.content = menu.renderLines(input.value).join('\n');
      return true;
    },
    isEmpty: () => input.value.trim() === '',
    focus: () => input.focus(),
    render(state: SessionState): void {
      const message = state.inputError;
      if (message === lastMessage) return;
      lastMessage = message;
      if (message === null) {
        hint.content = RESTING_HINT;
        hint.fg = currentTheme.textSubtle;
        box.borderColor = paneBorderColor(currentTheme, false);
        return;
      }
      // The glyph is the non-colour channel WCAG 1.4.1 asks for: the two
      // high-contrast themes have almost no palette, so the error colour on
      // its own would say nothing in them.
      hint.content = `✗ ${message}`;
      hint.fg = currentTheme.error;
      box.borderColor = currentTheme.error;
    },
    applyTheme(next: Theme): void {
      currentTheme = next;
      const hasError = lastMessage !== null;
      box.borderColor = hasError ? next.error : paneBorderColor(next, false);
      hint.fg = hasError ? next.error : next.textSubtle;
      input.textColor = next.textStrong;
      input.focusedTextColor = next.textStrong;
      suggestions.borderColor = next.border;
      suggestions.backgroundColor = next.selectedSurface;
      suggestionList.fg = next.textMuted;
      const previous = syntaxStyle;
      syntaxStyle = commandSyntaxStyle(next);
      commandStyleId = syntaxStyle.getStyleId('slash-command');
      input.syntaxStyle = syntaxStyle;
      previous.destroy();
      updateDecorations(input.value);
    },
    destroy(): void {
      input.off(InputRenderableEvents.INPUT, handleInput);
      input.off(InputRenderableEvents.ENTER, submit);
      if (!input.isDestroyed) input.syntaxStyle = null;
      syntaxStyle.destroy();
    },
  };
}
