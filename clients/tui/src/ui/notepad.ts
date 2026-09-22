import {BoxRenderable, type CliRenderer, TextareaRenderable, TextRenderable} from '@opentui/core';
import type {SessionState} from '../session-model.js';
import type {Theme} from './theme.js';

const HINT = 'F6: steer (unsent) · F7: chat (unsent) · Esc: close · / is inert';

// `generate_run_id` (`vs_project/_state.py`) builds ids as
// `{date}-{time}-{8-hex-suffix}-{slug}`: sortable and collision-safe, not
// meant for a person to read at a glance.
const RUN_ID_PATTERN = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}-[0-9a-f]{8}-(.+)$/;

/**
 * Where a run id matches the generator's shape, show the slug (the one part
 * an operator actually chose) and a plain UTC timestamp instead of the raw
 * digits. Anything that doesn't match — test fixtures, hand-written ids — is
 * shown as-is rather than mangled by a partial parse.
 */
export function formatRunLabel(runId: string): string {
  const match = RUN_ID_PATTERN.exec(runId);
  if (match === null) return runId;
  const [, year, month, day, hour, minute, slug] = match;
  return `${slug} · ${year}-${month}-${day} ${hour}:${minute} UTC`;
}

/**
 * The private operator notepad (#805). Deliberately a modal rather than a
 * new permanent pane, per the issue's own roadmap discussion: this is scratch
 * space for one run, not a surface worth a column all the time.
 *
 * The editor is a plain, unsubclassed `TextareaRenderable`: no `onSubmit`,
 * and nothing here ever calls `parseCommand`/`suggestSlashCommands` on its
 * text. OpenTUI's default textarea key bindings treat a bare Enter as
 * newline-insertion, so a line starting with `/` typed in here can never fire
 * as a command the way the same text would in the chat composer. That
 * composer's own interception of a leading `/` is a separate, pre-existing
 * behavior this change does not touch.
 *
 * The note's text never leaves this view on its own: it reaches the command
 * bar or the chat composer only through the two explicit promotion actions
 * (`session-controller.ts#promoteNoteToSteerDraft`/`promoteNoteToChatDraft`),
 * which pre-fill that composer's own buffer unsent, exactly as if the
 * operator had typed it there.
 */
export class NotepadView {
  readonly output: BoxRenderable;
  readonly #metaRun: TextRenderable;
  readonly #metaNote: TextRenderable;
  readonly #editor: TextareaRenderable;
  readonly #hint: TextRenderable;
  #renderedText: string | null = null;
  #renderedRunId: string | null = null;

  constructor(renderer: CliRenderer, theme: Theme) {
    this.output = new BoxRenderable(renderer, {
      id: 'notepad',
      width: '70%',
      height: '60%',
      position: 'absolute',
      left: '15%',
      top: '18%',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      // Square with an outer fill, the overlay exception (tui/conventions.md).
      borderStyle: 'single',
      borderColor: theme.info,
      backgroundColor: theme.canvas,
      title: ' Notepad ',
      visible: false,
      // Topmost: private scratch space the operator opened on purpose, so it
      // sits over the palette (35), the highest of the other overlays.
      zIndex: 40,
    });
    // Two tiers, the same split the header uses for its own metadata
    // (`header.ts#headerSpanStyle`): the run identity is the one fact this
    // line exists to report, so it is read at full `textPrimary` weight; the
    // note below it is supporting detail and recedes to `textMuted`. Neither
    // drops to `textSubtle`, which the theme reserves for punctuation and
    // rules rather than words (`theme.ts`).
    this.#metaRun = new TextRenderable(renderer, {
      id: 'notepad-meta-run',
      width: '100%',
      height: 1,
      flexShrink: 0,
      wrapMode: 'none',
      truncate: true,
      fg: theme.textPrimary,
    });
    this.#metaNote = new TextRenderable(renderer, {
      id: 'notepad-meta-note',
      content: 'Saved locally · kept out of run-events.jsonl',
      width: '100%',
      height: 1,
      flexShrink: 0,
      wrapMode: 'none',
      truncate: true,
      fg: theme.textMuted,
    });
    this.#editor = new TextareaRenderable(renderer, {
      id: 'notepad-editor',
      width: '100%',
      flexGrow: 1,
      wrapMode: 'word',
      placeholder: 'Private to this run. Never sent to an agent unless you promote it below.',
      textColor: theme.textPrimary,
      focusedTextColor: theme.textStrong,
      onContentChange: () => this.onTextChange(this.#editor.plainText),
    });
    this.#hint = new TextRenderable(renderer, {
      id: 'notepad-hint',
      content: HINT,
      fg: theme.textSubtle,
      width: '100%',
      height: 1,
      flexShrink: 0,
      wrapMode: 'none',
      truncate: true,
    });
    this.output.add(this.#metaRun);
    this.output.add(this.#metaNote);
    this.output.add(this.#editor);
    this.output.add(this.#hint);
  }

  /** Wired by the caller to `controller.setNoteText`; kept out of the constructor so tests can construct without a controller. */
  onTextChange: (text: string) => void = () => {};

  applyTheme(theme: Theme): void {
    this.output.borderColor = theme.info;
    this.output.backgroundColor = theme.canvas;
    this.#metaRun.fg = theme.textPrimary;
    this.#metaNote.fg = theme.textMuted;
    this.#hint.fg = theme.textSubtle;
    this.#editor.textColor = theme.textPrimary;
    this.#editor.focusedTextColor = theme.textStrong;
  }

  render(state: SessionState): void {
    const notepad = state.notepad;
    if (!notepad.open) {
      this.output.visible = false;
      return;
    }
    this.output.visible = true;
    // Synced from state only when it differs from what this view last wrote,
    // so a render triggered by the operator's own typing (which already
    // updated the widget directly) never resets the cursor mid-edit; this
    // only fires for text that arrived some other way, such as hydration
    // from `notes-store.ts` when the notepad first opens.
    if (this.#renderedText !== notepad.text) {
      this.#renderedText = notepad.text;
      if (this.#editor.plainText !== notepad.text) this.#editor.setText(notepad.text);
    }
    if (this.#renderedRunId !== state.runId) {
      this.#renderedRunId = state.runId;
      const run = state.runId === null ? 'unknown run' : formatRunLabel(state.runId);
      this.#metaRun.content = `Run ${run}`;
    }
  }

  focus(): void {
    this.#editor.focus();
  }
}
