import {titleCase} from '../derive.js';
import type {GraphColumn} from '../model.js';
import './Graph.css';

export interface GraphProps {
  round: number | null;
  columns: GraphColumn[];
}

/**
 * The selected round's agent pipeline, above the log and outside its scroller: a column per agent
 * kind in loop order, the agents of a kind stacked inside it, and an arrow to the next kind toned
 * by how that handover went. Adjacent agents that say the same thing arrive as one card with a
 * count. The arrows are drawn in CSS, so they stay out of the accessibility tree; the nodes are a
 * status display and take no focus.
 */
export function Graph({round, columns}: GraphProps) {
  if (round === null || columns.length === 0) return null;
  return (
    <section className="graph">
      <div className="col">
        <ol
          className="gflow"
          aria-label={`Round ${round} agents`}
          // biome-ignore lint/a11y/noNoninteractiveTabindex: the row scrolls sideways, and not every browser focuses a scroll container on its own.
          tabIndex={0}
        >
          {columns.map(column => (
            <li key={column.kind} className="gcol" data-edge={column.edge ?? undefined}>
              <ol className="gstack">
                {column.nodes.map(node => (
                  <li
                    key={node.id}
                    className="gnode"
                    data-status={node.status}
                    aria-current={node.status === 'active' ? 'step' : undefined}
                  >
                    <span className="grole trunc">
                      {node.role}
                      {node.count > 1 ? <span className="gn">{` ×${node.count}`}</span> : null}
                    </span>
                    <span className="gstat">{titleCase(node.status)}</span>
                    {node.runtime === null ? null : (
                      // One line: the tooltip carries the label whole when the card cuts it.
                      <span className="grun mono trunc" data-tip={node.runtime}>
                        {node.runtime}
                      </span>
                    )}
                  </li>
                ))}
              </ol>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
