package clidaemon_test

import (
	"context"
	"errors"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"google.golang.org/protobuf/proto"
	"testing"
	"time"
)

type fakeEnsurer struct {
	Calls    int
	Location clidaemon.Location
	Err      error
}

func (f *fakeEnsurer) Ensure(ctx context.Context, loc clidaemon.Location) error {
	if _, ok := ctx.Deadline(); !ok {
		panic("no deadline")
	}
	f.Calls++
	f.Location = loc
	return f.Err
}
func TestEnsureIsBoundedAndRunsOnce(t *testing.T) {
	loc := clidaemon.Location{XDGRuntimeDir: "/run/user/7", XDGStateHome: "/state", Home: "/home/user"}
	for _, reason := range []clidaemon.Reason{clidaemon.NoDaemon, clidaemon.DaemonTooOld, clidaemon.ClientTooOld, clidaemon.StreamLost} {
		f := &fakeEnsurer{}
		err := clidaemon.Ensure(deadlineContext{context.Background()}, f, loc, reason)
		allowed := reason == clidaemon.NoDaemon || reason == clidaemon.DaemonTooOld
		if allowed && (err != nil || f.Calls != 1 || f.Location != loc) || !allowed && (err == nil || f.Calls != 0) {
			t.Fatalf("reason %v: %v calls %d", reason, err, f.Calls)
		}
	}
	f := &fakeEnsurer{}
	if clidaemon.Ensure(context.Background(), f, loc, clidaemon.NoDaemon) == nil || f.Calls != 0 {
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
		Connect: func(context.Context) (clidaemon.Connection, error) {
			connections++
			if connections == 1 {
				return nil, &clidaemon.Unreadable{Reason: clidaemon.NoDaemon, Detail: "absent"}
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
			if u.Reason != clidaemon.StreamLost {
				t.Fatalf("failure %v", u)
			}
			if len(held) != 1 {
				t.Fatal("lost view")
			}
		},
	}
	err := follower.Follow(ctx, &pb.WatchTaskRequest{Context: &pb.CallContext{Repository: "/repo", Task: "task"}})
	if !errors.Is(err, context.Canceled) || runner.Calls != 1 || connections != 3 || changes != 2 || (len(held) != 1 || !proto.Equal(held[0], fact("j1", 1))) {
		t.Fatalf("Follow: %v ensure=%d connections=%d changes=%d", err, runner.Calls, connections, changes)
	}
}

func TestFollowerStopsOnTerminalHello(t *testing.T) {
	for _, tc := range []struct {
		name              string
		version           *pb.BuildVersions
		ensureErr         error
		reason            clidaemon.Reason
		connects, ensures int
	}{
		{"daemon old", &pb.BuildVersions{ProtocolVersion: 2, MinProtocolVersion: 1}, nil, clidaemon.DaemonTooOld, 2, 1},
		{"client old", &pb.BuildVersions{ProtocolVersion: 4, MinProtocolVersion: 4}, nil, clidaemon.ClientTooOld, 1, 0},
		{"cli missing", &pb.BuildVersions{ProtocolVersion: 2, MinProtocolVersion: 1}, &clidaemon.Unreadable{Reason: clidaemon.CLINotInstalled}, clidaemon.CLINotInstalled, 1, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fake := clidaemontest.New()
			fake.Versions = tc.version
			runner := &fakeEnsurer{Err: tc.ensureErr}
			calls, failures := 0, 0
			follower := clidaemon.Follower{Connect: func(context.Context) (clidaemon.Connection, error) { calls++; return fake, nil }, Ensurer: runner,
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
