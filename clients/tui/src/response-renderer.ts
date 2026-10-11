import type {ProtocolResponse, RequestInput} from '@vibesys/backend-client';
import {projectPerformance} from '@vibesys/core-state';
import {renderPerformanceCurve} from './performance-chart.js';

/** Renders the typed server replies used by generic command actions. */
export function renderCommandResponse(
  request: RequestInput,
  response: ProtocolResponse,
  responseView?: 'perf',
): string | null {
  if (response.ack) return `${response.ack.action}: ${response.ack.status}`;
  if (request.type !== 'query.performance' && responseView !== 'perf') return null;
  return renderPerformanceCurve(
    projectPerformance({
      performance: response.performance,
      events: response.events,
      objectiveDirection: response.performance_context?.objective_direction,
    }),
    response.performance_context,
  );
}
