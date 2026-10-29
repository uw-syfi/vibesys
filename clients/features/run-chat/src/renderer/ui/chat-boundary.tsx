import {Button, Row, Stack, Text} from '@vibesys/ui';
import {Component, type ReactNode} from 'react';

interface BoundaryProps {
  readonly children: ReactNode;
  /** The thread on screen: a failure ends when the panel shows another. */
  readonly resetKey: string | null;
  readonly close: () => void;
}

interface BoundaryState {
  readonly failed: boolean;
  readonly resetKey: string | null;
}

/**
 * Keeps a failure of the chat panel inside the panel. A render that throws shows a minimal error
 * state with a Close control, and the rest of the window keeps drawing.
 */
export class ChatBoundary extends Component<BoundaryProps, BoundaryState> {
  override state: BoundaryState = {failed: false, resetKey: this.props.resetKey};

  static getDerivedStateFromError(): Partial<BoundaryState> {
    return {failed: true};
  }

  static getDerivedStateFromProps(
    props: BoundaryProps,
    state: BoundaryState,
  ): Partial<BoundaryState> | null {
    return props.resetKey === state.resetKey ? null : {failed: false, resetKey: props.resetKey};
  }

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <Stack gap="space-3" grow>
        <Row justify="between" align="center" gap="space-3">
          <Text variant="small" tone="err">
            Chat failed
          </Text>
          <Button variant="quiet" size="sm" icon="cross" label="Close" press={this.props.close} />
        </Row>
      </Stack>
    );
  }
}
