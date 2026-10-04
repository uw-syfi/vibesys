/**
 * Presentation-only formatting for the CLI harness and model an agent
 * execution ran with. The backend emits `provider`/`model` as plain
 * configuration strings (e.g. `"codex"`, `"gpt-5.1-codex-max"`); this module
 * turns them into the label shown next to an agent node, e.g.
 * `"Codex (GPT 5.1 Codex Max)"`.
 */

const PROVIDER_LABELS: Record<string, string> = {
  codex: 'Codex',
  claude: 'Claude Code',
  gemini: 'Gemini',
  opencode: 'Opencode',
};

/** Segments that are acronyms rather than words, keyed by lowercase form. */
const KNOWN_ACRONYMS: Record<string, string> = {
  gpt: 'GPT',
};

/** `"Codex (GPT 5.1 Codex Max)"`; `null` when there is nothing to show. */
export function agentRuntimeLabel(
  provider: string | null | undefined,
  model: string | null | undefined,
): string | null {
  const providerText = provider?.trim() || null;
  const modelText = model?.trim() || null;
  if (providerText === null && modelText === null) return null;
  const providerLabel = providerText === null ? null : formatProvider(providerText);
  const modelLabel = modelText === null ? null : formatModel(modelText);
  if (providerLabel !== null && modelLabel !== null) return `${providerLabel} (${modelLabel})`;
  return providerLabel ?? modelLabel;
}

function formatProvider(provider: string): string {
  return PROVIDER_LABELS[provider.toLowerCase()] ?? titleCaseWords(provider);
}

function formatModel(model: string): string {
  return model
    .split('-')
    .filter(segment => segment.length > 0)
    .map(titleCaseSegment)
    .join(' ');
}

function titleCaseWords(text: string): string {
  return text
    .split(/[-_\s]+/)
    .filter(segment => segment.length > 0)
    .map(titleCaseSegment)
    .join(' ');
}

/** A digit-leading segment (e.g. `"5.1"`) is kept as-is; words are title-cased. */
function titleCaseSegment(segment: string): string {
  const acronym = KNOWN_ACRONYMS[segment.toLowerCase()];
  if (acronym !== undefined) return acronym;
  if (/^\d/.test(segment)) return segment;
  return segment.charAt(0).toUpperCase() + segment.slice(1).toLowerCase();
}
