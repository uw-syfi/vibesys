/**
 * The Runs dashboard's pure state: the tile fold (`tilesOf`, `groupTiles`, `needsYou`) over what the
 * app knows of each run, and the machine that holds how the runs are grouped.
 */

export {type GroupingContext, type GroupingEvent, groupingMachine} from './grouping.js';
export {
  type Attention,
  attentionOf,
  attentionWord,
  BASE,
  chartOf,
  durationWord,
  GROUP_BYS,
  type GroupBy,
  groupTiles,
  type IdeaInput,
  type Mark,
  type MeasuredBest,
  NEEDS_YOU,
  NOTE_MAX,
  needsYou,
  onMachine,
  type RunInput,
  ratioWord,
  STATUS_TONES,
  type StatusKind,
  type Tile,
  type TileChart,
  type TileGroup,
  type Tone,
  tileOf,
  tilesOf,
  UNKNOWN_GROUP,
  type Verdict,
} from './tiles.js';
