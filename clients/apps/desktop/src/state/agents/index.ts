/** The agents feature's reads of the state: the spawn tree and the transcript. */
export {type AgentName, agentNames, startOrder} from './names.js';
export {
  type AgentRow,
  type AgentRuntime,
  agentKey,
  type Branch,
  type FoldChoices,
  foldedCount,
  GROUP_FROM,
  type GroupRow,
  groupKey,
  isFolded,
  type SpawnRow,
  type SpawnState,
  type SpawnStats,
  type SpawnTree,
  spawnTree,
  type Tick,
  toggled,
} from './spawn-tree.js';
export {
  agentPlace,
  type Crumb,
  type Entry,
  type Party,
  type Transcript,
  type TurnOutcome,
  transcript,
} from './transcript.js';
