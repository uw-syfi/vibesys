import type {JsonResultPayload, ToolResultData} from '@vibesys/backend-client';

type ProtoValue = NonNullable<JsonResultPayload['value']>;
type ToolResultPayload = ToolResultData['payload'];

/** A protobuf `Value` as the plain JSON value it encodes. */
function valueToJson(value: ProtoValue | undefined): unknown {
  const kind = value?.kind;
  switch (kind?.case) {
    case 'nullValue':
      return null;
    case 'numberValue':
    case 'stringValue':
    case 'boolValue':
      return kind.value;
    case 'structValue':
      return Object.fromEntries(
        Object.entries(kind.value.fields).map(([key, field]) => [key, valueToJson(field)]),
      );
    case 'listValue':
      return kind.value.values.map(item => valueToJson(item));
    case undefined:
      return undefined;
  }
}

const MAX_TOOL_OUTPUT_LINES = 6;
const MAX_TOOL_OUTPUT_CHARACTERS = 600;
const MAX_PROMPT_LINES = 12;
const MAX_TOOL_ARG_LENGTH = 80;

export interface CollapsiblePreview {
  content: string;
  hiddenLines: number;
  hiddenCharacters: number;
  collapsible: boolean;
}

/**
 * The `/bin/bash -lc "<command>"` wrapper the codex CLI writes around the
 * argument of every `execute` call.
 *
 * Anchored at both ends and linear: the alternation is over literal shell
 * names, and the tail is a single greedy `[\s\S]+` with nothing after it, so
 * there is no nested quantifier for a long command to backtrack through.
 */
const SHELL_WRAPPER = /^(?:\S+\/)?(?:bash|zsh|sh|dash|fish)\s+-l?c\s+([\s\S]+)$/;

/** How much of a shell command the call line shows before eliding the rest. */
const MAX_COMMAND_LENGTH = 160;

/** How much of a first output line a collapsed result summary shows. */
const MAX_SUMMARY_LINE = 120;

/** How many files a multi-file change summary names before counting the rest. */
const MAX_LISTED_CHANGES = 2;

/** How many object keys a shape summary names before counting the rest. */
const MAX_SUMMARY_KEYS = 4;

/**
 * Formats the arguments of one known tool, or returns null to fall back to the
 * generic `key=value` rendering when the arguments are not the expected shape.
 */
type ToolCallFormatter = (args: Record<string, unknown>) => string | null;

/**
 * Verbs for the `kind` of a codex `file_change` entry.
 *
 * Both the past-participle and the bare-verb spelling appear on the wire
 * ("deleted" and "delete"), and the set is the provider's, not ours, so it is
 * a lookup table rather than an enum: an unrecognized kind is shown verbatim
 * instead of being dropped.
 */
const CHANGE_VERBS: Readonly<Record<string, string>> = {
  add: 'add',
  added: 'add',
  create: 'add',
  created: 'add',
  delete: 'delete',
  deleted: 'delete',
  remove: 'delete',
  removed: 'delete',
  modify: 'modify',
  modified: 'modify',
  update: 'modify',
  updated: 'modify',
  rename: 'rename',
  renamed: 'rename',
};

/**
 * Per-tool call renderers, keyed by lowercased tool name.
 *
 * The generic `key="value"` rendering is correct but unreadable for the two
 * shapes that dominate a transcript: a shell command wrapped in `bash -lc`,
 * and a patch whose whole argument is an array of file records. Lookup is a
 * single map hit per entry.
 */
const TOOL_CALL_FORMATTERS: ReadonlyMap<string, ToolCallFormatter> = new Map([
  // codex names it `execute` or `shell`; claude names it `Bash`.
  ['execute', shellCallSummary],
  ['shell', shellCallSummary],
  ['bash', shellCallSummary],
  ['run_command', shellCallSummary],
  ['apply_patch', changeCallSummary],
  ['file_change', changeCallSummary],
]);

/**
 * Removes the `bash -lc` wrapper and one matching pair of outer quotes,
 * leaving the command the agent actually meant to run.
 *
 * Exported because it is the whole of acceptance criterion 1 and is tested
 * directly against the wrapper spellings observed from codex.
 */
export function unwrapShellCommand(command: string): string {
  const match = SHELL_WRAPPER.exec(command.trim());
  if (match?.[1] === undefined) return command.trim();
  return stripOuterQuotes(match[1].trim());
}

function stripOuterQuotes(text: string): string {
  const quote = text[0];
  if ((quote === '"' || quote === "'") && text.length > 1 && text.endsWith(quote)) {
    return text.slice(1, -1);
  }
  return text;
}

/**
 * Shortens `text` to `limit` characters.
 *
 * Truncation happens before any quoting, never inside it: slicing a rendered
 * `key="value"` pair or a JSON serialization lands mid-string often enough
 * that the line reads as broken syntax.
 */
function truncateText(text: string, limit: number): string {
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
}

function shellCallSummary(args: Record<string, unknown>): string | null {
  const {command} = args;
  if (typeof command !== 'string' || command === '') return null;
  const rendered = truncateText(unwrapShellCommand(command), MAX_COMMAND_LENGTH);
  // claude's Bash tool carries a human summary beside the command; codex's
  // execute does not, so the comment is appended only when one exists.
  const {description} = args;
  return typeof description === 'string' && description !== ''
    ? `${rendered}  # ${description}`
    : rendered;
}

function changeCallSummary(args: Record<string, unknown>): string | null {
  const {changes} = args;
  if (!Array.isArray(changes)) return null;
  const summaries = changes
    .map(changeEntrySummary)
    .filter((summary): summary is string => summary !== null);
  if (summaries.length === 0) return null;
  if (summaries.length === 1) return summaries[0] ?? null;
  const listed = summaries.slice(0, MAX_LISTED_CHANGES);
  const overflow = summaries.length - listed.length;
  const tail = overflow > 0 ? `, +${overflow} more` : '';
  return `${summaries.length} changes: ${listed.join(', ')}${tail}`;
}

function changeEntrySummary(change: unknown): string | null {
  if (change === null || typeof change !== 'object') return null;
  const record = change as Record<string, unknown>;
  const rawPath = record['path'];
  if (typeof rawPath !== 'string' || rawPath === '') return null;
  const rawKind = record['kind'];
  const kind = typeof rawKind === 'string' ? rawKind.toLowerCase() : '';
  const verb = CHANGE_VERBS[kind] ?? (kind === '' ? 'change' : kind);
  return `${verb} ${rawPath}`;
}

/**
 * A one-line description of a value's shape: its top-level keys, or how many
 * elements it holds. Used both for a collapsed JSON result and for an argument
 * whose serialization is too long to show.
 */
export function jsonShapeSummary(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.length} item${value.length === 1 ? '' : 's'}]`;
  }
  if (value !== null && typeof value === 'object') {
    const keys = Object.keys(value);
    if (keys.length === 0) return '{}';
    const listed = keys.slice(0, MAX_SUMMARY_KEYS);
    const overflow = keys.length - listed.length;
    return `{keys: ${listed.join(', ')}${overflow > 0 ? `, +${overflow} more` : ''}}`;
  }
  if (typeof value === 'string') return `"${truncateText(value, MAX_TOOL_ARG_LENGTH)}"`;
  return JSON.stringify(value) ?? String(value);
}

/** The generic `key="value", key=json` rendering for a tool we know nothing about. */
function genericCallSummary(args: Record<string, unknown>): string {
  const parts = Object.entries(args).map(([key, value]) => {
    if (typeof value === 'string') return `${key}="${truncateText(value, MAX_TOOL_ARG_LENGTH)}"`;
    const serialized = JSON.stringify(value) ?? String(value);
    // An over-long serialization is replaced wholesale rather than sliced: a
    // cut lands inside a quoted key or string and leaves an unclosed quote.
    return `${key}=${serialized.length > MAX_TOOL_ARG_LENGTH ? jsonShapeSummary(value) : serialized}`;
  });
  return `(${parts.join(', ')})`;
}

export function toolCallPreview(tool: string, args: Record<string, unknown>): string {
  const summary = TOOL_CALL_FORMATTERS.get(tool.toLowerCase())?.(args) ?? null;
  return summary === null ? `→ ${tool}${genericCallSummary(args)}\n` : `→ ${tool} ${summary}\n`;
}

export function toolOutputPreview(
  content: string,
  expanded = false,
  maxLines = MAX_TOOL_OUTPUT_LINES,
  maxCharacters = MAX_TOOL_OUTPUT_CHARACTERS,
): CollapsiblePreview {
  return collapsePreview(formatJsonObject(content), expanded, maxLines, maxCharacters);
}

/**
 * Renders a tool result from its typed payload when the backend preserved
 * one, falling back to the string-sniffing path for older event logs.
 */
export function toolResultPreview(
  content: string,
  payload: ToolResultPayload | null | undefined,
  expanded = false,
): CollapsiblePreview {
  if (payload == null || payload.case === undefined) return toolOutputPreview(content, expanded);
  const full = formatToolResultPayload(payload, content);
  const summary = payloadSummary(payload, full);
  if (summary === null) {
    return collapsePreview(full, expanded, MAX_TOOL_OUTPUT_LINES, MAX_TOOL_OUTPUT_CHARACTERS);
  }
  return summarizedPreview(full, summary, expanded);
}

/**
 * Renders a tool result from its typed payload.
 *
 * The expanded form is what the reader gets after asking for the whole thing:
 * a command lays out stdout, a separated stderr section, and its exit code;
 * a JSON value is pretty-printed. Both are word-wrapped by the caller.
 */
function formatToolResultPayload(
  payload: Exclude<ToolResultPayload, {case: undefined}>,
  fallback: string,
): string {
  if (payload.case === 'json') {
    return JSON.stringify(valueToJson(payload.value.value), null, 2) ?? fallback;
  }
  if (payload.case === 'command') {
    const command = payload.value;
    const sections: string[] = [];
    if (command.stdout !== '') sections.push(command.stdout.replace(/\n+$/, ''));
    // A blank line before the label so a failure's stderr reads as its own
    // section rather than as the tail of stdout.
    if (command.stderr !== '') {
      const separated = sections.length > 0 ? '\n' : '';
      sections.push(`${separated}stderr:\n${command.stderr.replace(/\n+$/, '')}`);
    }
    if (command.exitCode !== undefined) sections.push(`exit code: ${command.exitCode}`);
    if (sections.length > 0) return sections.join('\n');
  }
  return fallback;
}

/**
 * The single line a collapsed typed result shows, or null when the payload
 * carries nothing better than the generic head-of-output truncation.
 *
 * A command reports its status, wall time, and output size; a JSON value
 * reports its shape. Both answer "did this work, and how much is there" in one
 * row, which is what the reader scanning a transcript is asking.
 */
function payloadSummary(
  payload: Exclude<ToolResultPayload, {case: undefined}>,
  full: string,
): string | null {
  if (payload.case === 'json') return jsonShapeSummary(valueToJson(payload.value.value));
  const command = payload.value;
  // An empty command payload fell back to the raw content, which has no shape
  // to summarize.
  if (command.stdout === '' && command.stderr === '' && command.exitCode === undefined) {
    return null;
  }
  const parts: string[] = [];
  if (command.exitCode !== undefined) parts.push(`exit ${command.exitCode}`);
  if (command.duration !== undefined) parts.push(`${command.duration.toFixed(1)}s`);
  // Without an exit code or a wall time there is no status to report, and a
  // bare size says nothing about what happened, so the output speaks for
  // itself instead.
  if (parts.length === 0) {
    const first = firstNonEmptyLine(full);
    if (first !== '') parts.push(truncateText(first, MAX_SUMMARY_LINE));
  }
  const lines = countLines(command.stdout) + countLines(command.stderr);
  if (lines > 0) parts.push(`${lines} line${lines === 1 ? '' : 's'}`);
  return parts.join(' · ');
}

function countLines(text: string): number {
  const trimmed = text.replace(/\n+$/, '');
  return trimmed === '' ? 0 : trimmed.split('\n').length;
}

function firstNonEmptyLine(text: string): string {
  return text.split('\n').find(line => line.trim() !== '') ?? '';
}

/**
 * Collapses to `summary` rather than to the head of `full`.
 *
 * The hidden counts stay in the units `#renderToolTurn` reports, so the
 * summary line still advertises the whole payload as available behind Enter.
 */
function summarizedPreview(full: string, summary: string, expanded: boolean): CollapsiblePreview {
  const lines = full.split('\n');
  if (lines.at(-1) === '') lines.pop();
  const body = lines.join('\n');
  const collapsible =
    lines.length > MAX_TOOL_OUTPUT_LINES || body.length > MAX_TOOL_OUTPUT_CHARACTERS;
  if (expanded || !collapsible) {
    return {content: body, hiddenLines: 0, hiddenCharacters: 0, collapsible};
  }
  return {
    content: summary,
    hiddenLines: lines.length,
    hiddenCharacters: Math.max(0, body.length - summary.length),
    collapsible: true,
  };
}

function collapsePreview(
  formatted: string,
  expanded: boolean,
  maxLines: number,
  maxCharacters: number,
): CollapsiblePreview {
  const lines = formatted.split('\n');
  if (lines.at(-1) === '') lines.pop();
  const full = lines.join('\n');
  const collapsible = lines.length > maxLines || full.length > maxCharacters;
  if (expanded || !collapsible) {
    return {content: full, hiddenLines: 0, hiddenCharacters: 0, collapsible};
  }

  const lineLimited = lines.slice(0, maxLines).join('\n');
  const preview = lineLimited.slice(0, maxCharacters).trimEnd();
  const visibleLines = preview === '' ? 0 : preview.split('\n').length;
  return {
    content: preview,
    hiddenLines: Math.max(0, lines.length - visibleLines),
    hiddenCharacters: Math.max(0, full.length - preview.length),
    collapsible,
  };
}

function formatJsonObject(content: string): string {
  const trimmed = content.trim();
  if (!(trimmed.startsWith('{') || trimmed.startsWith('['))) return content;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed === null || typeof parsed !== 'object') return content;
    return JSON.stringify(parsed, null, 2);
  } catch {
    return content;
  }
}

export function promptPreview(
  content: string,
  expanded: boolean,
  maxLines = MAX_PROMPT_LINES,
): {content: string; hiddenLines: number} {
  const lines = content.split('\n');
  if (lines.at(-1) === '') lines.pop();
  const hiddenLines = Math.max(0, lines.length - maxLines);
  return {
    content: expanded || hiddenLines === 0 ? content : lines.slice(0, maxLines).join('\n'),
    hiddenLines,
  };
}

export function elapsedLabel(elapsedMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}
