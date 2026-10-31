package server_test

import (
	"context"
	"fmt"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
)

// runStream is a subscription to the runs topic, read as the run list's frames always
// were: an entry, a removal, the marker that the first snapshot is complete, or a beat.
// The assertions on a run's head, liveness and project therefore read the same.
type runStream struct {
	stream apiv1.HostService_SubscribeClient
}

// runFrame is one frame of a runs subscription. A frame that is not an entry carries an
// empty entry, so a test reads its head without a nil check.
type runFrame struct {
	*apiv1.RunEntry
	RunId           string
	Removed         bool
	InitialComplete bool
	Heartbeat       bool
}

func watchRuns(ctx context.Context, c apiv1.HostServiceClient) (*runStream, error) {
	stream, err := c.Subscribe(ctx, &apiv1.SubscribeRequest{Topic: &apiv1.SubscribeRequest_Runs{Runs: &apiv1.RunsTopic{}}})
	if err != nil {
		return nil, err
	}
	return &runStream{stream}, nil
}

func (r *runStream) Recv() (*runFrame, error) {
	frame, err := r.stream.Recv()
	if err != nil {
		return nil, err
	}
	none := &apiv1.RunEntry{}
	switch each := frame.Frame.(type) {
	case *apiv1.SubscribeResponse_Entry:
		return &runFrame{RunEntry: each.Entry.GetRuns(), RunId: each.Entry.Key}, nil
	case *apiv1.SubscribeResponse_Removed:
		return &runFrame{RunEntry: none, RunId: each.Removed, Removed: true}, nil
	case *apiv1.SubscribeResponse_Synced:
		return &runFrame{RunEntry: none, InitialComplete: true}, nil
	case *apiv1.SubscribeResponse_Heartbeat:
		return &runFrame{RunEntry: none, Heartbeat: true}, nil
	case *apiv1.SubscribeResponse_Failed:
		return nil, fmt.Errorf("runs topic failed: %v", each.Failed)
	}
	return nil, fmt.Errorf("unknown frame %v", frame)
}

func (f *runFrame) GetRunId() string         { return f.RunId }
func (f *runFrame) GetInitialComplete() bool { return f.InitialComplete }
