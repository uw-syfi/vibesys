package clidaemon_test

import (
	"context"
	"errors"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"google.golang.org/protobuf/proto"
	"testing"
	"time"
)

type fakeEnsurer struct {
	Calls int
	Err   error
}

func (f *fakeEnsurer) Ensure(ctx context.Context) (clidaemon.Socket, error) {
	if _, ok := ctx.Deadline(); !ok {
		panic("no deadline")
	}
	f.Calls++
	if f.Err != nil {
		return clidaemon.Socket{}, f.Err
	}
	return clidaemon.SocketFrom("/run/user/7/vibesys-cli/daemon.sock")
}
func TestEnsureIsBoundedAndRunsOnce(t *testing.T) {
	for _, reason := range []core.ViewReason{core.NoDaemon, core.DaemonTooOld, core.ClientTooOld, core.StreamLost} {
		f := &fakeEnsurer{}
		socket, err := clidaemon.Ensure(deadlineContext{context.Background()}, f, reason)
		allowed := reason == core.NoDaemon || reason == core.DaemonTooOld
		if allowed && (err != nil || f.Calls != 1 || socket.Path() == "") || !allowed && (err == nil || f.Calls != 0) {
			t.Fatalf("reason %v: %v calls %d", reason, err, f.Calls)
		}
	}
	f := &fakeEnsurer{}
	if _, err := clidaemon.Ensure(context.Background(), f, core.NoDaemon); err == nil || f.Calls != 0 {
		t.Fatal("unbounded ensure ran")
	}
}
func TestFollowerReconnectsWithCommittedTokenAndKeepsView(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	f := clidaemontest.New()
	f.SetFacts([]*pb.TaskFact{fact("j1", 1)})
	runner := &fakeEnsurer{}
	connections, sleeps, changes := 0, 0, 0
	var held []*pb.TaskFact
	follower := clidaemon.Follower{
		Connect: func(context.Context, clidaemon.Socket) (clidaemon.Connection, error) {
			connections++
			if connections == 2 {
				return nil, &clidaemon.Unreadable{Reason: core.NoDaemon, Detail: "absent"}
			}
			return f, nil
		},
		Ensurer: runner,
		Bound: func(ctx context.Context) (context.Context, context.CancelFunc) {
			return deadlineContext{ctx}, func() {}
		},
		Sleep: func(context.Context, time.Duration) error {
			sleeps++
			if sleeps == 2 {
				cancel()
				return context.Canceled
			}
			return nil
		},
		Changed: func(facts []*pb.TaskFact) { changes++; held = facts },
		Failed: func(u *clidaemon.Unreadable) {
			if u.Reason != core.StreamLost {
				t.Fatalf("failure %v", u)
			}
			if len(held) != 1 {
				t.Fatal("lost view")
			}
		},
	}
	err := follower.Follow(ctx, &pb.WatchTaskRequest{Context: &pb.CallContext{Repository: "/repo", Task: "task"}})
	if !errors.Is(err, context.Canceled) || runner.Calls != 2 || connections != 3 || changes != 2 || (len(held) != 1 || !proto.Equal(held[0], fact("j1", 1))) {
		t.Fatalf("Follow: %v ensure=%d connections=%d changes=%d", err, runner.Calls, connections, changes)
	}
}

func TestFollowerStopsOnTerminalHello(t *testing.T) {
	for _, tc := range []struct {
		name              string
		version           *pb.BuildVersions
		ensureErr         error
		reason            core.ViewReason
		connects, ensures int
	}{
		{"daemon old", &pb.BuildVersions{ProtocolVersion: 2, MinProtocolVersion: 1}, nil, core.DaemonTooOld, 1, 1},
		{"client old", &pb.BuildVersions{ProtocolVersion: 4, MinProtocolVersion: 4}, nil, core.ClientTooOld, 1, 1},
		{"cli missing", &pb.BuildVersions{ProtocolVersion: 2, MinProtocolVersion: 1}, &clidaemon.Unreadable{Reason: core.CLINotInstalled}, core.CLINotInstalled, 0, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fake := clidaemontest.New()
			fake.Versions = tc.version
			runner := &fakeEnsurer{Err: tc.ensureErr}
			calls, failures := 0, 0
			follower := clidaemon.Follower{Connect: func(context.Context, clidaemon.Socket) (clidaemon.Connection, error) { calls++; return fake, nil }, Ensurer: runner,
				Bound: func(ctx context.Context) (context.Context, context.CancelFunc) {
					return deadlineContext{ctx}, func() {}
				},
				Sleep:   func(context.Context, time.Duration) error { t.Fatal("terminal outcome retried"); return nil },
				Changed: func([]*pb.TaskFact) {}, Failed: func(*clidaemon.Unreadable) { failures++ }}
			err := follower.Follow(context.Background(), &pb.WatchTaskRequest{})
			var u *clidaemon.Unreadable
			if !errors.As(err, &u) || u.Reason != tc.reason || calls != tc.connects || runner.Calls != tc.ensures || failures != 1 {
				t.Fatalf("error %v, connects %d, ensures %d, failures %d", err, calls, runner.Calls, failures)
			}
		})
	}
}
