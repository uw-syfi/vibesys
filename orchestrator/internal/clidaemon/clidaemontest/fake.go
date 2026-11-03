// Package clidaemontest provides the CLI link's deterministic service Fake and
// reusable contract. A Fake owns facts; each stream computes a diff on Recv.
package clidaemontest

import (
	"buf.build/go/protovalidate"
	"context"

	"io"

	"strings"
	"sync"

	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Fake implements the same protocol negotiation and reset/diff semantics as
// the daemon. Recv returns EOF when quiescent rather than waiting for real time.
// SetFacts is a whole store revision, so streams cannot observe torn snapshots.
type Fake struct {
	mu              sync.Mutex
	facts           []*pb.TaskFact
	Epoch           string
	Versions        *pb.BuildVersions
	RejectComponent bool
}

func New() *Fake {
	return &Fake{Epoch: "E1", Versions: &pb.BuildVersions{ProtocolVersion: 3, MinProtocolVersion: 1}}
}
func (f *Fake) Close() error { return nil }
func (f *Fake) SetFacts(facts []*pb.TaskFact) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.facts = cloneFacts(facts)
}
func cloneFacts(facts []*pb.TaskFact) []*pb.TaskFact {
	out := make([]*pb.TaskFact, 0, len(facts))
	for _, v := range facts {
		out = append(out, proto.Clone(v).(*pb.TaskFact))
	}
	return out
}
func (f *Fake) Hello(ctx context.Context, r *pb.HelloRequest) (*pb.HelloResponse, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if f.RejectComponent || (r.GetComponent() != pb.Component_COMPONENT_CLI && r.GetComponent() != pb.Component_COMPONENT_ORCHESTRATOR) {
		return nil, status.Error(codes.InvalidArgument, "component is not a CLI client")
	}
	compat := pb.Compatibility_COMPATIBILITY_COMPATIBLE
	if r.GetVersions().GetProtocolVersion() < f.Versions.MinProtocolVersion {
		compat = pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD
	} else if f.Versions.ProtocolVersion < r.GetVersions().GetMinProtocolVersion() {
		compat = pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD
	}
	return &pb.HelloResponse{Versions: proto.Clone(f.Versions).(*pb.BuildVersions), Compatibility: compat}, nil
}
func (f *Fake) WatchTask(ctx context.Context, r *pb.WatchTaskRequest) (clidaemon.Stream, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := protovalidate.Validate(r); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if r.GetContext().GetRepository() == "" || r.GetContext().GetTask() == "" || r.JudgedCommits > 100 {
		return nil, status.Error(codes.InvalidArgument, "WatchTask context or judged_commits")
	}
	return &stream{fake: f, ctx: ctx}, nil
}

type stream struct {
	fake     *Fake
	ctx      context.Context
	sequence uint64
	sent     map[string]*pb.TaskFact
	queue    []*pb.WatchTaskResponse
}

func keyed(facts []*pb.TaskFact) map[string]*pb.TaskFact {
	out := map[string]*pb.TaskFact{}
	for _, f := range facts {
		k, err := clidaemon.FactKey(f)
		if err != nil {
			panic(err)
		}
		out[k] = f
	}
	return out
}
func (s *stream) Recv() (*pb.WatchTaskResponse, error) {
	if err := s.ctx.Err(); err != nil {
		return nil, err
	}
	if len(s.queue) == 0 {
		s.fake.mu.Lock()
		facts := cloneFacts(s.fake.facts)
		epoch := s.fake.Epoch
		s.fake.mu.Unlock()
		next := keyed(facts)
		reset := s.sent == nil
		for k := range s.sent {
			if next[k] == nil && !strings.HasPrefix(k, "judgment/") {
				reset = true
			}
		}
		var diff []*pb.TaskFact
		for _, v := range facts {
			k, _ := clidaemon.FactKey(v)
			if !proto.Equal(v, s.sent[k]) {
				diff = append(diff, v)
			}
		}
		if len(diff) > 500 {
			reset = true
		}
		add := func(m *pb.WatchTaskResponse) {
			s.sequence++
			m.Epoch = epoch
			m.Sequence = s.sequence
			s.queue = append(s.queue, m)
		}
		if reset {
			add(&pb.WatchTaskResponse{Body: &pb.WatchTaskResponse_Reset_{Reset_: &pb.Reset{}}})
			diff = facts
		}
		for len(diff) > 0 {
			n := min(500, len(diff))
			add(&pb.WatchTaskResponse{Body: &pb.WatchTaskResponse_Facts{Facts: &pb.TaskFacts{Facts: diff[:n]}}})
			diff = diff[n:]
		}
		if reset {
			add(&pb.WatchTaskResponse{Body: &pb.WatchTaskResponse_CaughtUp{CaughtUp: &pb.CaughtUp{}}})
		}
		s.sent = next
	}
	if len(s.queue) == 0 {
		return nil, io.EOF
	}
	m := s.queue[0]
	s.queue = s.queue[1:]
	return m, nil
}
