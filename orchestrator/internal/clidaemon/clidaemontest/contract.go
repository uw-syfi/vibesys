package clidaemontest

import (
	"context"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"testing"
	"time"
)

// A fixed deadline lets the same suite call bounded unary clients without
// consulting a clock or waiting for a timeout in the deterministic tier.
type contractContext struct{ context.Context }

func (contractContext) Deadline() (time.Time, bool) {
	return time.Date(2100, 1, 1, 0, 0, 0, 0, time.UTC), true
}

// Contract runs the service's portable contract against a fresh isolated task.
// A real-tier factory may launch a built CLI; the deterministic tier uses New.
func Contract(t *testing.T, newService func(*testing.T) clidaemon.Service) {
	t.Helper()
	ContractWithContext(t, contractContext{context.Background()}, newService)
}

// ContractWithContext runs the contract with a caller-provided hang guard.
func ContractWithContext(t *testing.T, ctx context.Context, newService func(*testing.T) clidaemon.Service) {
	t.Helper()
	t.Run("Hello", func(t *testing.T) {
		s := newService(t)
		for _, component := range []pb.Component{pb.Component_COMPONENT_CLI, pb.Component_COMPONENT_ORCHESTRATOR} {
			r, err := s.Hello(ctx, &pb.HelloRequest{Component: component, Versions: &pb.BuildVersions{ProtocolVersion: 3, MinProtocolVersion: 3}})
			if err != nil || r.GetCompatibility() != pb.Compatibility_COMPATIBILITY_COMPATIBLE {
				t.Fatalf("Hello: %v, %v", r, err)
			}
		}
		_, err := s.Hello(ctx, &pb.HelloRequest{Component: pb.Component_COMPONENT_SUPERVISOR})
		if status.Code(err) != codes.InvalidArgument {
			t.Fatalf("unknown component: %v", err)
		}
	})
	t.Run("ResetOnReconnect", func(t *testing.T) {
		s := newService(t)
		ctx, cancel := context.WithCancel(ctx)
		defer cancel()
		request := &pb.WatchTaskRequest{Context: &pb.CallContext{Repository: "/repo", Task: "task"}}
		var session clidaemon.Session
		for pass := 0; pass < 2; pass++ {
			token := session.Token()
			request.Epoch, request.AfterSequence = token.Epoch, token.Sequence
			stream, err := s.WatchTask(ctx, request)
			if err != nil {
				t.Fatal(err)
			}
			// Bound the message count as a guard, never wait on a timer.
			complete := false
			for i := 0; i < 10000; i++ {
				m, err := stream.Recv()
				if err != nil {
					t.Fatal(err)
				}
				if i == 0 && m.GetReset_() == nil {
					t.Fatal("protocol 3 must reset")
				}
				if _, err := session.Apply(m); err != nil {
					t.Fatal(err)
				}
				if m.GetCaughtUp() != nil {
					complete = true
					break
				}
			}
			if !complete {
				t.Fatal("reset did not complete")
			}
			session.Disconnected()
		}
	})
	t.Run("InvalidWindow", func(t *testing.T) {
		stream, err := newService(t).WatchTask(ctx, &pb.WatchTaskRequest{Context: &pb.CallContext{Repository: "/repo", Task: "task"}, JudgedCommits: 101})
		if err == nil {
			_, err = stream.Recv()
		}
		if status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid window: %v", err)
		}
	})
}
