import type {NewRunContext, NewRunEvent} from '../../state/index.js';

type Dispatch = (event: NewRunEvent) => void;

/** What every page of the form takes: the machine's context and where its events go. */
export interface CardProps {
  readonly context: NewRunContext;
  readonly dispatch: Dispatch;
}
