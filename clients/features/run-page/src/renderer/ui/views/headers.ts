/**
 * Each view's pane header (`ViewHeader`), the one place the frame reads them from. A view that adds
 * to its header exports `<view>Header` from its own module and replaces its line here with a
 * re-export, for example `export {stepHeader} from './step/step-view.js';`. Until then a view adds
 * nothing and the frame's header stands.
 */
import type {AgentPaneProps, AgentsViewProps} from './agents/agents-view.js';
import type {PaneHeader, ViewHeader} from './props.js';
import type {TimelineViewProps} from './timeline/timeline-view.js';

const NONE = (): PaneHeader => ({});

export const timelineHeader: ViewHeader<TimelineViewProps> = NONE;
export const agentsHeader: ViewHeader<AgentsViewProps> = NONE;
export const agentHeader: ViewHeader<AgentPaneProps> = NONE;
export {ideaHeader} from './idea/idea-view.js';
export {logsHeader} from './logs/logs-view.js';
export {itemHeader, stepHeader} from './step/step-view.js';
