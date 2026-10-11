/**
 * The one runtime validator for generated JSON Schema descriptors.
 *
 * Every wire contract the clients read is a Pydantic model on the server, exported as a JSON Schema
 * descriptor (`generated/*.schema.ts`) that this walker enforces before a value is asserted to its
 * generated TypeScript type. A walk is either:
 *
 * - **open** (the run protocol, #869): a newer server may add fields, closed-set members, and union
 *   members without a protocol version change, so unknown ones pass while known ones are checked.
 * - **strict** (the host helper, whose models are `extra="forbid"` and version-negotiated by
 *   `hello`): an unknown field, closed-set member, or union tag is an error naming its path.
 *
 * String length, item count, pattern, and numeric bounds are enforced in both modes; the server's
 * generator refuses any keyword this walker does not implement.
 */
import {BackendClientError} from './errors.js';

export type Schema = Record<string, unknown>;

export interface SchemaWalk {
  /** The descriptor documents whose `$defs` a `$ref` may name, searched in order. */
  readonly documents: readonly Schema[];
  /** Reject unknown fields, closed-set members, and union tags. */
  readonly strict: boolean;
}

const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|([+-])(\d{2}):(\d{2}))$/;
// RFC 3339 admits :60 only at an announced UTC leap second. These are every
// insertion month through the most recent leap second in December 2016.
const RFC3339_LEAP_SECONDS = new Set([
  '1972-06',
  '1972-12',
  '1973-12',
  '1974-12',
  '1975-12',
  '1976-12',
  '1977-12',
  '1978-12',
  '1979-12',
  '1981-06',
  '1982-06',
  '1983-06',
  '1985-06',
  '1987-12',
  '1989-12',
  '1990-12',
  '1992-06',
  '1993-06',
  '1994-06',
  '1995-12',
  '1997-06',
  '1998-12',
  '2005-12',
  '2008-12',
  '2012-06',
  '2015-06',
  '2016-12',
]);

/** Validate `value` against `schema`, or throw a `parse` error naming the first offending path. */
export function validateSchema(
  value: unknown,
  schema: Schema,
  path: string,
  walk: SchemaWalk,
): void {
  const ref = schema['$ref'];
  if (typeof ref === 'string') {
    validateSchema(value, schemaReference(ref, walk), path, walk);
    return;
  }
  const constant = schema['const'];
  if (constant !== undefined) {
    validateConstant(value, constant, path, walk);
    return;
  }
  const choices = schema['anyOf'];
  if (Array.isArray(choices)) {
    validateAnyOf(value, choices, path, walk);
    return;
  }
  const alternatives = schema['oneOf'];
  const discriminator = schema['discriminator'];
  if (Array.isArray(alternatives) && isRecord(discriminator)) {
    validateTaggedUnion(value, discriminator, path, walk);
    return;
  }
  validateTypedSchema(value, schema, path, walk);
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function requireRecord(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new BackendClientError('parse', `Invalid server ${path}: expected an object`);
  }
  return value;
}

export function fieldError(path: string, key: string, expected: string): BackendClientError {
  return new BackendClientError('parse', `Invalid server ${path}: ${key} must be ${expected}`);
}

function invalid(path: string, problem: string): BackendClientError {
  return new BackendClientError('parse', `Invalid server ${path}: ${problem}`);
}

function schemaReference(ref: string, walk: SchemaWalk): Schema {
  const name = ref.startsWith('#/$defs/') ? ref.slice('#/$defs/'.length) : ref;
  for (const document of walk.documents) {
    const definition = (document as {$defs?: Record<string, Schema>}).$defs?.[name];
    if (definition !== undefined) return definition;
  }
  throw new BackendClientError('parse', `Invalid protocol schema ref: #/$defs/${name}`);
}

function validateConstant(value: unknown, constant: unknown, path: string, walk: SchemaWalk): void {
  // In an open walk, string literals are closed-set members, which stay forward compatible.
  // Numeric literals are protocol-version boundaries and must agree exactly.
  if (!walk.strict && typeof constant === 'string' && typeof value === 'string') return;
  if (value === constant) return;
  throw invalid(path, `must be ${JSON.stringify(constant)}`);
}

function validateAnyOf(value: unknown, choices: unknown[], path: string, walk: SchemaWalk): void {
  let mismatch: BackendClientError | undefined;
  for (const choice of choices) {
    if (!isRecord(choice)) continue;
    try {
      validateSchema(value, choice, path, walk);
      return;
    } catch (error) {
      if (!(error instanceof BackendClientError)) throw error;
      mismatch ??= error;
    }
  }
  throw mismatch ?? invalid(path, 'does not match its protocol shape');
}

function validateTaggedUnion(
  value: unknown,
  discriminator: Record<string, unknown>,
  path: string,
  walk: SchemaWalk,
): void {
  const record = requireRecord(value, path);
  const property = discriminator['propertyName'];
  const mapping = discriminator['mapping'];
  if (typeof property !== 'string' || !isRecord(mapping)) {
    throw new BackendClientError('parse', `Invalid protocol schema discriminator at ${path}`);
  }
  const tag = record[property];
  if (typeof tag !== 'string') throw fieldError(path, property, 'a string');
  const reference = mapping[tag];
  if (typeof reference !== 'string') {
    // #869: a newer run server may add a union member without changing the protocol version. Its
    // tag and object shape are still checked, but only a known member can be validated deeply.
    if (walk.strict) throw fieldError(path, property, `one of ${Object.keys(mapping).join(', ')}`);
    return;
  }
  validateSchema(value, {$ref: reference}, path, walk);
}

function validateTypedSchema(value: unknown, schema: Schema, path: string, walk: SchemaWalk): void {
  const type = schema['type'];
  switch (type) {
    case 'array':
      validateArray(value, schema, path, walk);
      return;
    case 'object':
      validateObject(value, schema, path, walk);
      return;
    case 'null':
      validatePrimitive(value === null, type, path);
      return;
    case 'string':
      validateString(value, schema, path, walk);
      return;
    case 'number':
      validateNumber(value, schema, path, false);
      return;
    case 'integer':
      validateNumber(value, schema, path, true);
      return;
    case 'boolean':
      validatePrimitive(typeof value === 'boolean', type, path);
  }
}

function validateString(value: unknown, schema: Schema, path: string, walk: SchemaWalk): void {
  if (typeof value !== 'string') {
    validatePrimitive(false, 'string', path);
    return;
  }
  const members = schema['enum'];
  if (walk.strict && Array.isArray(members) && !members.includes(value)) {
    throw invalid(path, `must be one of ${members.join(', ')}`);
  }
  const {minLength, maxLength, pattern} = schema;
  const length = [...value].length;
  if (typeof minLength === 'number' && length < minLength) {
    throw invalid(path, `must be at least ${minLength} characters`);
  }
  if (typeof maxLength === 'number' && length > maxLength) {
    throw invalid(path, `must be at most ${maxLength} characters`);
  }
  if (typeof pattern === 'string' && !new RegExp(pattern, 'u').test(value)) {
    throw invalid(path, `must match ${pattern}`);
  }
  if (schema['format'] === 'date-time' && !isRfc3339DateTime(value)) {
    throw invalid(path, 'must be a date-time');
  }
}

function validateNumber(value: unknown, schema: Schema, path: string, integer: boolean): void {
  const type = integer ? 'integer' : 'number';
  if (typeof value !== 'number' || (integer && !Number.isInteger(value))) {
    validatePrimitive(false, type, path);
    return;
  }
  const {minimum, maximum, exclusiveMinimum} = schema;
  if (typeof minimum === 'number' && value < minimum) {
    throw invalid(path, `must be at least ${minimum}`);
  }
  if (typeof exclusiveMinimum === 'number' && value <= exclusiveMinimum) {
    throw invalid(path, `must be greater than ${exclusiveMinimum}`);
  }
  if (typeof maximum === 'number' && value > maximum) {
    throw invalid(path, `must be at most ${maximum}`);
  }
}

function validatePrimitive(valid: boolean, type: string, path: string): void {
  if (valid) return;
  throw invalid(path, `must be a ${type}`);
}

function validateArray(value: unknown, schema: Schema, path: string, walk: SchemaWalk): void {
  if (!Array.isArray(value)) throw invalid(path, 'must be an array');
  const {minItems, maxItems, prefixItems, items} = schema;
  if (typeof minItems === 'number' && value.length < minItems) {
    throw invalid(path, `must have at least ${minItems} items`);
  }
  if (typeof maxItems === 'number' && value.length > maxItems) {
    throw invalid(path, `must have at most ${maxItems} items`);
  }
  const prefix = Array.isArray(prefixItems) ? prefixItems : [];
  for (const [index, item] of value.entries()) {
    const itemSchema: unknown = index < prefix.length ? prefix[index] : items;
    if (isRecord(itemSchema)) validateSchema(item, itemSchema, `${path}[${index}]`, walk);
  }
}

function validateObject(value: unknown, schema: Schema, path: string, walk: SchemaWalk): void {
  const record = requireRecord(value, path);
  validateRequired(record, schema['required'], path);
  const properties = isRecord(schema['properties']) ? schema['properties'] : {};
  if (walk.strict && schema['additionalProperties'] === false) {
    for (const key of Object.keys(record)) {
      if (!(key in properties)) throw invalid(`${path}.${key}`, 'is not a known field');
    }
  }
  // #869 (open walks): newly added server fields must not make an older client disconnect, so only
  // known fields are validated; the generated types retain the known contract.
  for (const [key, child] of Object.entries(properties)) {
    if (key in record && isRecord(child))
      validateSchema(record[key], child, `${path}.${key}`, walk);
  }
}

function validateRequired(record: Record<string, unknown>, required: unknown, path: string): void {
  if (!Array.isArray(required)) return;
  for (const key of required) {
    if (typeof key === 'string' && !(key in record)) throw fieldError(path, key, 'present');
  }
}

function isRfc3339DateTime(value: string): boolean {
  const parts = RFC3339_DATE_TIME.exec(value);
  if (parts === null) return false;
  const [
    ,
    yearText,
    monthText,
    dayText,
    hourText,
    minuteText,
    secondText,
    offsetSign,
    offsetHourText,
    offsetMinuteText,
  ] = parts;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const offsetHour = offsetHourText === undefined ? 0 : Number(offsetHourText);
  const offsetMinute = offsetMinuteText === undefined ? 0 : Number(offsetMinuteText);
  return (
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= daysInMonth(year, month) &&
    hour <= 23 &&
    minute <= 59 &&
    offsetHour <= 23 &&
    offsetMinute <= 59 &&
    (second <= 59 ||
      (second === 60 &&
        isAnnouncedLeapSecond({
          year,
          month,
          day,
          hour,
          minute,
          offsetSign,
          offsetHour,
          offsetMinute,
        })))
  );
}

interface DateTimeParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  offsetSign: string | undefined;
  offsetHour: number;
  offsetMinute: number;
}

function isAnnouncedLeapSecond({
  year,
  month,
  day,
  hour,
  minute,
  offsetSign,
  offsetHour,
  offsetMinute,
}: DateTimeParts): boolean {
  const direction = offsetSign === '-' ? -1 : 1;
  const offsetMilliseconds = direction * (offsetHour * 60 + offsetMinute) * 60_000;
  // Date.UTC treats years 0 through 99 as 1900 through 1999. Set the full year
  // explicitly so an ancient date cannot borrow a modern leap-second entry.
  const utc = new Date(0);
  utc.setUTCFullYear(year, month - 1, day);
  utc.setUTCHours(hour, minute, 59, 0);
  utc.setTime(utc.getTime() - offsetMilliseconds);
  const utcMonth = `${utc.getUTCFullYear()}-${String(utc.getUTCMonth() + 1).padStart(2, '0')}`;
  return (
    utc.getUTCDate() === daysInMonth(utc.getUTCFullYear(), utc.getUTCMonth() + 1) &&
    utc.getUTCHours() === 23 &&
    utc.getUTCMinutes() === 59 &&
    RFC3339_LEAP_SECONDS.has(utcMonth)
  );
}

function daysInMonth(year: number, month: number): number {
  if (month === 2) return isLeapYear(year) ? 29 : 28;
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

function isLeapYear(year: number): boolean {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
}
