/**
 * What usage records add up to: grouped by role, harness, model, round or run, and over time. The
 * fold reads only `UsageRecord`s, so it serves one run and, later, the records of many.
 */
export {
  addRecord,
  dimensionValue,
  foldUsage,
  type IdeaOf,
  NO_USAGE,
  totalUsage,
  USAGE_DIMENSIONS,
  type UsageBucket,
  type UsageDimension,
  type UsageFold,
  type UsageGroup,
  type UsageOverTime,
  type UsageTotals,
  usageOverTime,
} from './fold.js';
export {compactTokens} from './words.js';
