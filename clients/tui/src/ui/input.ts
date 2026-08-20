import {
  BoxRenderable,
  type CliRenderer,
  InputRenderable,
  InputRenderableEvents,
  SyntaxStyle,
  TextRenderable,
} from '@opentui/core';
import {
  type CommandContext,
  type SlashCommand,
  slashCommandRange,
  suggestSlashCommands,
} from '../commands.js';
import type {Theme} from './theme.js';

export interface InputPanel {
  box: BoxRenderable;
  suggestions: BoxRenderable;
  /** Narrows the completions to the commands the current view offers. */
  setCommandContext(context: CommandContext): void;
  completeSuggestion(): boolean;
  /** True when nothing is typed, so Enter belongs to whatever pane is behind. */
  isEmpty(): boolean;
  focus(): void;
  applyTheme(theme: Theme): void;
  destroy(): void;
}

function commandSyntaxStyle(theme: Theme): SyntaxStyle {
  return SyntaxStyle.fromStyles({'slash-command': {fg: theme.accent, bold: true}});
}

/**
 * The docked chat's own input. It carries no command suggestions: the command
 * list belongs to the client's input, and this box is for questions about the
 * experiment on screen. A slash command typed here still runs, through the same
 * path as anywhere else.
 */
export interface ChatInputPanel {
  box: BoxRenderable;
  isEmpty(): boolean;
  focus(): void;
  setFocused(focused: boolean): void;
  applyTheme(theme: Theme): void;
  destroy(): void;
}

export function createChatInputPanel(
  renderer: CliRenderer,
  onSubmit: (value: string) => void,
  theme: Theme,
): ChatInputPanel {
  const box = new BoxRenderable(renderer, {
    // The modal chat owns 'chat-input-box'; this is the docked one.
    id: 'chat-dock-input-box',
    height: 3,
    width: '100%',
    border: true,
    borderStyle: 'rounded',
    borderColor: theme.border,
    title: ' Chat ',
    paddingLeft: 1,
    paddingRight: 1,
  });
  const input = new InputRenderable(renderer, {
    id: 'chat-dock-input',
    width: '100%',
    placeholder: 'Type a question',
    textColor: theme.textStrong,
    focusedTextColor: theme.textStrong,
  });
  const submit = (value: string): void => {
    input.value = '';
    onSubmit(value);
  };
  input.on(InputRenderableEvents.ENTER, submit);
  box.add(input);
  let focused = false;
  let current = theme;
  return {
    box,
    isEmpty: () => input.value.trim() === '',
    focus: () => input.focus(),
    // Which box the keystrokes land in is the one thing two inputs must never
    // leave ambiguous, so the focused one carries the focus border.
    setFocused(next: boolean): void {
      focused = next;
      box.borderColor = next ? current.borderFocus : current.border;
    },
    applyTheme(next: Theme): void {
      current = next;
      box.borderColor = focused ? next.borderFocus : next.border;
      input.textColor = next.textStrong;
      input.focusedTextColor = next.textStrong;
    },
    destroy(): void {
      input.off(InputRenderableEvents.ENTER, submit);
    },
  };
}

export function createInputPanel(
  renderer: CliRenderer,
  onSubmit: (value: string) => void,
  theme: Theme,
): InputPanel {
  const box = new BoxRenderable(renderer, {
    id: 'input-box',
    height: 3,
    width: '100%',
    border: true,
    borderStyle: 'rounded',
    borderColor: theme.success,
    title: ' Ask or command ',
    paddingLeft: 1,
    paddingRight: 1,
  });
  let syntaxStyle = commandSyntaxStyle(theme);
  let commandStyleId = syntaxStyle.getStyleId('slash-command');
  const input = new InputRenderable(renderer, {
    id: 'input',
    width: '100%',
    placeholder: 'Type a question or /help',
    textColor: theme.textStrong,
    focusedTextColor: theme.textStrong,
    syntaxStyle,
  });
  const suggestions = new BoxRenderable(renderer, {
    id: 'input-suggestions',
    position: 'absolute',
    bottom: 3,
    left: 0,
    width: '100%',
    height: 3,
    visible: false,
    zIndex: 5,
    border: true,
    borderStyle: 'rounded',
    borderColor: theme.border,
    backgroundColor: theme.selectedSurface,
    paddingLeft: 1,
    paddingRight: 1,
  });
  const suggestionList = new TextRenderable(renderer, {
    id: 'input-suggestion-list',
    width: '100%',
    height: 1,
    fg: theme.textMuted,
    wrapMode: 'none',
    truncate: true,
    content: '',
  });
  suggestions.add(suggestionList);
  let matches: readonly SlashCommand[] = [];
  let context: CommandContext = {};

  const updateDecorations = (value: string): void => {
    input.clearAllHighlights();
    const range = slashCommandRange(value);
    if (range !== null && commandStyleId !== null) {
      input.addHighlightByCharRange({...range, styleId: commandStyleId});
    }

    matches = suggestSlashCommands(value, context);
    const visible = matches.length > 0;
    suggestions.visible = visible;
    suggestions.height = matches.length + 2;
    suggestionList.height = Math.max(1, matches.length);
    suggestionList.content = matches
      .map(
        (command, index) =>
          `${index === 0 ? '›' : ' '} ${command.name.padEnd(10)} ${command.description}${
            index === 0 && command.name !== value ? '  [Tab]' : ''
          }`,
      )
      .join('\n');
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
      const suggestion = matches[0];
      if (suggestion === undefined || suggestion.name === input.value) return false;
      input.value = suggestion.name;
      return true;
    },
    isEmpty: () => input.value.trim() === '',
    focus: () => input.focus(),
    applyTheme(next: Theme): void {
      box.borderColor = next.success;
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
