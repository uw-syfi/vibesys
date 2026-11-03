import type {Fault} from './fault.js';

/** Where one subscription (the run stream, or one agent's) stands, apart from the connection. */
export type FeedStatus =
  | {readonly _tag: 'Opening'}
  | {readonly _tag: 'Live'}
  | {readonly _tag: 'Restarting'; readonly why: 'gap' | 'fell-behind' | 'reconnect'}
  | {readonly _tag: 'Failed'; readonly fault: Fault};
