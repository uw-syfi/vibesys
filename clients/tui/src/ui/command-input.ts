import {
  BoxRenderable,
  type CliRenderer,
  InputRenderable,
  InputRenderableEvents,
  SyntaxStyle,
  TextRenderable,
} from '@opentui/core';
import {type CommandContext, slashCommandRange, suggestSlashCommands} from '../commands.js';
import {paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import {SuggestionMenu} from './suggestion-menu.js';
import type {Theme} from './theme.js';

export interface CommandInputPanel {
  box: BoxRenderable;
  suggestions: BoxRenderable;
  /** Narrows the completions to the commands the current view offers. */
  setCommandContext(context: CommandContext): void;
  completeSuggestion(): boolean;
  navigateSuggestions(direction: 1 | -1): boolean;
  /** True when nothing is typed, so Enter belongs to whatever pane is behind. */
  isEmpty(): boolean;
  focus(): void;
  applyTheme(theme: Theme): void;
  destroy(): void;
}

const COMMAND_TITLE = 'Command';

function commandSyntaxStyle(theme: Theme): SyntaxStyle {
  return SyntaxStyle.fromStyles({'slash-command': {fg: theme.accent, bold: true}});
}

export function createCommandInputPanel(
  renderer: CliRenderer,
  onSubmit: (value: string) => void,
  theme: Theme,
  /** Called when the box is clicked, so the pane focus follows the cursor. */
  onFocusRequest: () => void = () => {},
): CommandInputPanel {
  const box = new BoxRenderable(renderer, {
    id: 'command-input-box',
    height: 3,
    width: '100%',
    border: true,
    // The focus treatment names the one pane the navigation keys are on. This
    // box is drawn inside that pane rather than being one, so it never wears
    // the treatment: resting frame, resting colour, and the gutter cell where a
    // pane would put its marker. A second lit border made the marked pane
    // ambiguous, which is the whole complaint behind #433. It still takes the
    // title from `focus.ts` so its label sits at the same column as the pane
    // holding it, and as the chat's `Message` box across the landing view.
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
    bottom: 3,
    left: 0,
    width: '100%',
    height: 3,
    visible: false,
    zIndex: 5,
    border: true,
    // Square, because this popup keeps its fill and a box may have one or the
    // other, never both (tui-conventions.md). It is absolutely positioned over
    // the panes above the command column, and the fill is what stops them
    // showing through it.
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
  const submit = (value: string): void => {
    input.value = '';
    onSubmit(value);
  };
  input.on(InputRenderableEvents.INPUT, updateDecorations);
  input.on(InputRenderableEvents.ENTER, submit);
  box.add(input);
  return {
    box,
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
    applyTheme(next: Theme): void {
      box.borderColor = paneBorderColor(next, false);
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
      input.off(InputRenderableEvents.INPUT, updateDecorations);
      input.off(InputRenderableEvents.ENTER, submit);
      if (!input.isDestroyed) input.syntaxStyle = null;
      syntaxStyle.destroy();
    },
  };
}
