// Package probe checks that a target is ready: every planned operation must succeed once per
// round, each attempt on a fresh connection, before a deadline.
package probe

import (
	"context"
	"fmt"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/call"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/engine"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/plan"
)

// retry is the pause between failed attempts.
const retry = 100 * time.Millisecond

// Run sends each operation of p until it succeeded rounds times, in plan order. It returns nil
// when all did, or an error naming the first operation that had not succeeded when ctx ended.
func Run(ctx context.Context, sender engine.Sender, p *plan.Plan, rounds int) error {
	if rounds < 1 {
		return fmt.Errorf("--rounds must be at least 1")
	}
	for i := range p.Ops {
		op := &p.Ops[i]
		for round := 1; round <= rounds; round++ {
			last := ""
			for {
				outcome := sender.Do(ctx, op)
				if outcome.Error == pb.ErrorKind_ERROR_KIND_UNSPECIFIED {
					break
				}
				last = describe(outcome)
				timer := time.NewTimer(retry)
				select {
				case <-ctx.Done():
					timer.Stop()
					return fmt.Errorf("not ready: operation %d (%s) round %d of %d did not succeed before the deadline; last attempt: %s",
						op.Index, p.Names[op.Operation], round, rounds, last)
				case <-timer.C:
				}
			}
		}
	}
	return nil
}

func describe(outcome call.Outcome) string {
	if len(outcome.Steps) == 0 {
		return outcome.Error.String()
	}
	step := outcome.Steps[len(outcome.Steps)-1]
	message := fmt.Sprintf("step %d: %s", len(outcome.Steps)-1, step.GetError())
	if step.GetStatus() != 0 {
		message += fmt.Sprintf(" (HTTP %d)", step.GetStatus())
	}
	if step.GetErrorMessage() != "" {
		message += ": " + step.GetErrorMessage()
	}
	return message
}
