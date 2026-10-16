/**
 * A structured reply read as fields to show: the reply's own JSON Schema (`AgentReply.json_schema`,
 * from the role's reply model) says which fields are text, which are a choice from a closed set and
 * which are lists, and its value fills them in. Nothing here names a role's fields, so a reply of
 * any kind, including one this code has never seen, reads the same way: text as prose, a choice as
 * a badge, short values as facts, lists as lists, nested records as groups, and a field with no
 * value not at all.
 */
import type {Tone} from './views.js';

type Json = Readonly<Record<string, unknown>>;

/** One field to show; the same shapes `@vibesys/ui`'s `ReplyField` draws. */
export type ReplyFieldView =
  | {readonly kind: 'prose'; readonly key: string; readonly label: string; readonly text: string}
  | {
      readonly kind: 'badge';
      readonly key: string;
      readonly label: string;
      readonly value: string;
      readonly tone?: Tone;
    }
  | {readonly kind: 'fact'; readonly key: string; readonly label: string; readonly value: string}
  | {
      readonly kind: 'list';
      readonly key: string;
      readonly label: string;
      readonly items: readonly string[];
    }
  | {
      readonly kind: 'group';
      readonly key: string;
      readonly label: string;
      readonly fields: readonly ReplyFieldView[];
    };

/** How far nested records are followed before the rest is shown as one JSON fact. */
const MAX_DEPTH = 4;

const GOOD = new Set(['pass', 'passed', 'accepted', 'success', 'succeeded', 'supported', 'ok']);
const BAD = new Set([
  'fail',
  'failed',
  'rejected',
  'error',
  'blocked',
  'disproven',
  'implementation_failed',
]);

/** A choice's tone from its word; a word that is neither good nor bad has none. */
function toneOf(value: string): Tone | undefined {
  const word = value.toLowerCase();
  if (GOOD.has(word)) return 'ok';
  if (BAD.has(word)) return 'err';
  return undefined;
}

/** `judge_result` as `Judge result`, and `JudgeResponse` as `Judge response`. */
function humanize(name: string): string {
  const spaced = name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .trim()
    .toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** The kind of a reply as a title: `JudgeResponse` reads as `Judge response`. */
export function replyTitle(kind: string): string {
  return humanize(kind);
}

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** The schema `schema` stands for: a `$ref` followed, and an optional or single-choice union opened. */
function resolve(schema: unknown, defs: Json): Json {
  let current: unknown = schema;
  for (let hops = 0; hops < 8 && isRecord(current); hops += 1) {
    const ref = current['$ref'];
    if (typeof ref === 'string') {
      current = defs[ref.slice(ref.lastIndexOf('/') + 1)];
      continue;
    }
    const members = current['anyOf'] ?? current['oneOf'] ?? current['allOf'];
    if (!Array.isArray(members)) break;
    const real = members.filter(member => !(isRecord(member) && member['type'] === 'null'));
    if (real.length !== 1) break;
    current = real[0];
  }
  return isRecord(current) ? current : {};
}

function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined || value === '') return true;
  if (Array.isArray(value)) return value.length === 0;
  return isRecord(value) && Object.keys(value).length === 0;
}

function scalarText(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function isScalar(value: unknown): boolean {
  return ['string', 'number', 'boolean'].includes(typeof value);
}

function fieldFor(
  key: string,
  schema: Json,
  value: unknown,
  defs: Json,
  depth: number,
): ReplyFieldView | null {
  if (isEmpty(value)) return null;
  const label = humanize(key);
  const resolved = resolve(schema, defs);
  if (typeof value === 'string') {
    if (Array.isArray(resolved['enum']) || 'const' in resolved) {
      const tone = toneOf(value);
      return {kind: 'badge', key, label, value, ...(tone === undefined ? {} : {tone})};
    }
    return {kind: 'prose', key, label, text: value};
  }
  if (typeof value === 'boolean') return {kind: 'fact', key, label, value: value ? 'Yes' : 'No'};
  if (typeof value === 'number') return {kind: 'fact', key, label, value: String(value)};
  if (Array.isArray(value)) return arrayField(key, label, resolved, value, defs, depth);
  if (isRecord(value)) return recordField(key, label, resolved, value, defs, depth);
  return {kind: 'fact', key, label, value: scalarText(value)};
}

function arrayField(
  key: string,
  label: string,
  schema: Json,
  items: readonly unknown[],
  defs: Json,
  depth: number,
): ReplyFieldView | null {
  if (items.every(isScalar)) return {kind: 'list', key, label, items: items.map(scalarText)};
  const itemSchema = resolve(schema['items'], defs);
  const fields = items.flatMap((item, index) => {
    const own = fieldFor(`${key} ${index + 1}`, itemSchema, item, defs, depth + 1);
    return own === null ? [] : [own];
  });
  return fields.length === 0 ? null : {kind: 'group', key, label, fields};
}

function recordField(
  key: string,
  label: string,
  schema: Json,
  value: Json,
  defs: Json,
  depth: number,
): ReplyFieldView | null {
  if (depth >= MAX_DEPTH) return {kind: 'fact', key, label, value: JSON.stringify(value)};
  const fields = fieldsOf(schema, value, defs, depth + 1);
  return fields.length === 0 ? null : {kind: 'group', key, label, fields};
}

/** The fields of `value` in the schema's order, then any key the schema does not name. */
function fieldsOf(schema: Json, value: Json, defs: Json, depth: number): ReplyFieldView[] {
  const properties = isRecord(schema['properties']) ? schema['properties'] : {};
  const keys = [...Object.keys(properties), ...Object.keys(value).filter(k => !(k in properties))];
  return keys.flatMap(key => {
    if (!(key in value)) return [];
    const field = fieldFor(key, resolve(properties[key], defs), value[key], defs, depth);
    return field === null ? [] : [field];
  });
}

/** The fields of one reply, read through its model's JSON Schema. */
export function replyFields(schema: Json, value: Json): readonly ReplyFieldView[] {
  const defs = isRecord(schema['$defs']) ? schema['$defs'] : {};
  return fieldsOf(resolve(schema, defs), value, defs, 0);
}
