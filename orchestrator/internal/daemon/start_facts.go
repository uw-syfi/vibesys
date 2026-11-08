package daemon

import (
	"context"
	"errors"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/fit"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// startFacts establishes what a start records on its run, before the command is decided: the task's
// card, read from the repository the start runs in, and the eval place, resolved and checked for fit.
// A place that cannot be used is part of the result (the core refuses it with a receipt); a task that
// cannot be read, or a call that failed, is an error of the call.
func (s *Service) startFacts(ctx context.Context, start core.StartRun) (core.StartFacts, error) {
	card, err := s.readCard(ctx, TaskRef{Source: TaskCommitted, Repository: start.Repository, Task: start.Task})
	if err != nil {
		return core.StartFacts{}, err
	}
	return s.resolvePlace(ctx, card, start.EvalPlace)
}

// resolvePlace resolves the eval place for a task's card, as a start and a completed setup both do.
func (s *Service) resolvePlace(ctx context.Context, card *core.TaskCard, chosen string) (core.StartFacts, error) {
	var need *core.AcceleratorNeed
	if card != nil {
		need = card.Hardware
	}
	var verdicts []core.PlaceVerdict
	if need != nil || chosen != "" {
		// The rule of fit is internal/fit's alone: the verdict of each place is its answer, and the
		// core holds the verdicts as data.
		message := wire.NeedMessage(need)
		places, err := s.places(ctx)
		if err != nil {
			return core.StartFacts{}, err
		}
		for _, p := range places {
			verdicts = append(verdicts, core.PlaceVerdict{Name: p.GetName(), Fits: need != nil && fit.Fits(message, p)})
		}
	}
	return core.ResolveStartFacts(card, chosen, verdicts), nil
}

// readCard reads the card of the task as committed at the reference. A host with no reader, and a task the reader
// cannot find, give no card, which the run records as such. A task whose files are invalid or ask for
// what the host cannot honor is refused with the reader's detail, which names the key or the path.
func (s *Service) readCard(ctx context.Context, ref TaskRef) (*core.TaskCard, error) {
	if s.Tasks == nil {
		return nil, nil
	}
	card, err := bound(s.Bounds, ctx, func(c context.Context) (core.TaskCard, error) { return s.Tasks.Read(c, ref) })
	var unreadable *TaskUnreadable
	switch {
	case err == nil:
		return &card, nil
	case errors.As(err, &unreadable) && unreadable.Reason == TaskNotFound:
		return nil, nil
	case errors.As(err, &unreadable):
		return nil, status.Error(codes.InvalidArgument, unreadable.Error())
	}
	return nil, status.Errorf(codes.Unavailable, "read the card of task %s: %v", ref.Task, err)
}

func (s *Service) places(ctx context.Context) ([]*pb.EvalPlace, error) {
	if s.Places == nil {
		return nil, nil
	}
	places, err := bound(s.Bounds, ctx, func(c context.Context) ([]*pb.EvalPlace, error) { return s.Places.Places(c, false) })
	if err != nil {
		return nil, status.Errorf(codes.Unavailable, "list the eval places: %v", err)
	}
	return places, nil
}
