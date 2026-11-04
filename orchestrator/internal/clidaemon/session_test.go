package clidaemon_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"io/fs"
	"math/rand/v2"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

type fakeFS struct {
	info clidaemon.Directory
	err  error
	path string
}

func (f *fakeFS) LstatDirectory(p string) (clidaemon.Directory, error) {
	f.path = p
	return f.info, f.err
}
func TestLocateGolden(t *testing.T) {
	raw, err := os.ReadFile("../../../proto/vibesys/cli/v1/conformance/locate.golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var golden struct {
		Cases []struct {
			Name   string
			Env    map[string]string
			UID    int
			Socket string
		}
	}
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatal(err)
	}
	for _, tc := range golden.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			f := &fakeFS{info: clidaemon.Directory{Mode: fs.ModeDir | 0700, UID: tc.UID}}
			socket, err := clidaemon.Locate(clidaemon.Location{XDGRuntimeDir: tc.Env["XDG_RUNTIME_DIR"]}, tc.UID, f)
			if err != nil || socket.Path() != tc.Socket {
				t.Fatalf("Locate: %v, %v", socket, err)
			}
		})
	}
}
func TestLocateRejectsUnsafeDirectories(t *testing.T) {
	for _, info := range []clidaemon.Directory{{Mode: fs.ModeSymlink | 0700}, {Mode: 0700}, {Mode: fs.ModeDir | 0755}, {Mode: fs.ModeDir | 0700, UID: 1}, {Mode: fs.ModeDir | 0700, Network: true}} {
		_, err := clidaemon.Locate(clidaemon.Location{}, 0, &fakeFS{info: info})
		var u *clidaemon.Unreadable
		if !errors.As(err, &u) || u.Reason != clidaemon.NoDaemon {
			t.Fatalf("accepted %v: %v", info, err)
		}
	}
	for _, loc := range []clidaemon.Location{{}, {XDGRuntimeDir: "/" + strings.Repeat("x", 100)}} {
		if _, err := clidaemon.Locate(loc, 0, &fakeFS{err: fs.ErrNotExist}); err == nil {
			t.Fatal("missing/oversized accepted")
		}
	}
}

// deadlineContext supplies a deterministic deadline without a wall-clock timer.
type deadlineContext struct{ context.Context }

func (deadlineContext) Deadline() (time.Time, bool) {
	return time.Date(2100, 1, 1, 0, 0, 0, 0, time.UTC), true
}
func TestHelloProtocolAndFailures(t *testing.T) {
	raw, err := os.ReadFile("../../../proto/vibesys/cli/v1/conformance/protocol.json")
	if err != nil {
		t.Fatal(err)
	}
	var p struct {
		Since uint32 `json:"watch_task_since"`
	}
	if err := json.Unmarshal(raw, &p); err != nil || p.Since != clidaemon.RequiredDaemonProtocol {
		t.Fatal("protocol drift")
	}
	ctx := deadlineContext{context.Background()}
	for _, tc := range []struct {
		protocol, min uint32
		reject        bool
		want          clidaemon.Reason
	}{{3, 1, false, 0}, {2, 1, false, clidaemon.DaemonTooOld}, {4, 4, false, clidaemon.ClientTooOld}, {2, 1, true, clidaemon.DaemonTooOld}} {
		f := clidaemontest.New()
		f.Versions = &pb.BuildVersions{ProtocolVersion: tc.protocol, MinProtocolVersion: tc.min}
		f.RejectComponent = tc.reject
		err := clidaemon.Hello(ctx, f)
		if tc.want == 0 {
			if err != nil {
				t.Fatal(err)
			}
			continue
		}
		var u *clidaemon.Unreadable
		if !errors.As(err, &u) || u.Reason != tc.want {
			t.Fatalf("Hello: %v", err)
		}
	}
	if clidaemon.Hello(context.Background(), clidaemontest.New()) == nil {
		t.Fatal("unbounded Hello")
	}
}
func fact(id string, n uint32) *pb.TaskFact {
	return &pb.TaskFact{Fact: &pb.TaskFact_Job{Job: &pb.Job{Id: id, State: pb.JobState(n%6 + 1)}}}
}
func frame(seq uint64, body any) *pb.WatchTaskResponse {
	m := &pb.WatchTaskResponse{Epoch: "E1", Sequence: seq}
	switch b := body.(type) {
	case *pb.Reset:
		m.Body = &pb.WatchTaskResponse_Reset_{Reset_: b}
	case *pb.CaughtUp:
		m.Body = &pb.WatchTaskResponse_CaughtUp{CaughtUp: b}
	case []*pb.TaskFact:
		m.Body = &pb.WatchTaskResponse_Facts{Facts: &pb.TaskFacts{Facts: b}}
	}
	return m
}
func apply(t *testing.T, s *clidaemon.Session, m *pb.WatchTaskResponse) {
	t.Helper()
	if _, err := s.Apply(m); err != nil {
		t.Fatal(err)
	}
}
func TestResetCutsAndResumeProperties(t *testing.T) {
	r := rand.New(rand.NewPCG(11, 19))
	for trial := 0; trial < 300; trial++ {
		var s clidaemon.Session
		apply(t, &s, frame(1, &pb.Reset{}))
		apply(t, &s, frame(2, []*pb.TaskFact{fact("old", 1)}))
		apply(t, &s, frame(3, &pb.CaughtUp{}))
		s.Disconnected()
		held, token := s.Facts(), s.Token()
		n := r.IntN(30) + 1
		cut := r.IntN(n + 2)
		apply(t, &s, frame(1, &pb.Reset{}))
		var want []*pb.TaskFact
		for i := 0; i < n; i++ {
			want = append(want, fact(fmt.Sprint(i), r.Uint32()))
		}
		for i := 0; i < min(cut, n); i++ {
			apply(t, &s, frame(uint64(i+2), want[i:i+1]))
		}
		if !reflect.DeepEqual(s.Facts(), held) || s.Token() != token {
			t.Fatal("torn reset")
		}
		if cut > n {
			apply(t, &s, frame(uint64(n+2), &pb.CaughtUp{}))
			if len(s.Facts()) != n {
				t.Fatal("incomplete committed reset")
			}
		} else {
			s.Disconnected()
			if !reflect.DeepEqual(s.Facts(), held) || s.Token() != token {
				t.Fatal("cut lost committed state")
			}
		}
		s.Disconnected()
		// Every reconnect can reset, regardless of the prior token.
		apply(t, &s, frame(1, &pb.Reset{}))
		apply(t, &s, frame(2, want))
		apply(t, &s, frame(3, &pb.CaughtUp{}))
		apply(t, &s, frame(4, want))
		once := s.Facts()
		apply(t, &s, frame(5, want))
		if !reflect.DeepEqual(once, s.Facts()) || s.Token().Sequence != 3 {
			t.Fatal("diff is not idempotent or advanced token")
		}
		if _, err := s.Apply(frame(7, want)); err == nil {
			t.Fatal("gap accepted")
		}
	}
}

func FuzzResetResume(f *testing.F) {
	f.Add(uint16(501), uint16(1), true)
	f.Add(uint16(2), uint16(2), false)
	f.Fuzz(func(t *testing.T, size, cut uint16, resume bool) {
		n := int(size%1100) + 1
		daemon := clidaemontest.New()
		facts := make([]*pb.TaskFact, n)
		for i := range facts {
			facts[i] = fact(fmt.Sprint(i), uint32(i))
		}
		daemon.SetFacts(facts)
		request := &pb.WatchTaskRequest{Context: &pb.CallContext{Repository: "/repo", Task: "task"}}
		stream, err := daemon.WatchTask(context.Background(), request)
		if err != nil {
			t.Fatal(err)
		}
		var session clidaemon.Session
		// Cut at any boundary of a reset, including a batch boundary above 500.
		messages := (n+499)/500 + 2
		for i := 0; i < int(cut)%messages; i++ {
			m, err := stream.Recv()
			if err != nil {
				t.Fatal(err)
			}
			apply(t, &session, m)
		}
		session.Disconnected()
		request.Epoch, request.AfterSequence = session.Token().Epoch, session.Token().Sequence
		stream, err = daemon.WatchTask(context.Background(), request)
		if err != nil {
			t.Fatal(err)
		}
		for i := 0; i < messages; i++ {
			m, err := stream.Recv()
			if err != nil {
				t.Fatal(err)
			}
			apply(t, &session, m)
		}
		if len(session.Facts()) != n {
			t.Fatal("reset lost facts")
		}
		token := session.Token()
		session.Disconnected()
		if resume {
			request.Epoch, request.AfterSequence = token.Epoch, token.Sequence
			stream, err = daemon.WatchTask(context.Background(), request)
			if err != nil {
				t.Fatal(err)
			}
			for i := 0; i < messages; i++ {
				m, err := stream.Recv()
				if err != nil {
					t.Fatal(err)
				}
				apply(t, &session, m)
			}
			if session.Token() != token || len(session.Facts()) != n {
				t.Fatal("resume reset lost state")
			}
		}
	})
}
