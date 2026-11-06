package clientcli_test

import (
	"bytes"
	"context"
	"errors"
	"net"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Every exit code has a case that ends in it, and no verb ends in a code outside the table.
func TestExitCodes(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	frames := []*pb.WatchRunsResponse{snapshotFrame(1, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})}
	tooOld := func(c pb.Compatibility) func() (*pb.HelloResponse, error) {
		return func() (*pb.HelloResponse, error) {
			return &pb.HelloResponse{Daemon: &pb.BuildVersions{BinaryVersion: "9.9", ProtocolVersion: 2, MinProtocolVersion: 2}, Compatibility: c}, nil
		}
	}
	cases := []struct {
		name    string
		service *fakeService
		args    []string
		code    int
		stderr  string
	}{
		{"list", &fakeService{frames: frames}, []string{"run", "list"}, clientcli.ExitOK, ""},
		{"help", &fakeService{}, []string{"--help"}, clientcli.ExitOK, ""},
		{"no verb", &fakeService{}, nil, clientcli.ExitUsage, "a verb is needed"},
		{"unknown verb", &fakeService{}, []string{"run", "dance"}, clientcli.ExitUsage, "not a verb"},
		{"not run", &fakeService{}, []string{"dance"}, clientcli.ExitUsage, "run VERB"},
		{"unknown flag", &fakeService{}, []string{"run", "list", "--nope"}, clientcli.ExitUsage, "nope"},
		{"start without a task", &fakeService{}, []string{"run", "start"}, clientcli.ExitUsage, "--task"},
		{"bad harness", &fakeService{}, []string{"run", "start", "--task", "t", "--harness", "vim"}, clientcli.ExitUsage, "claude_code or codex"},
		{"bad deadline", &fakeService{}, []string{"run", "start", "--task", "t", "--deadline", "soon"}, clientcli.ExitUsage, "RFC 3339"},
		{"steer without text", &fakeService{frames: frames}, []string{"run", "steer"}, clientcli.ExitUsage, "TEXT"},
		{"list with an argument", &fakeService{frames: frames}, []string{"run", "list", "x"}, clientcli.ExitUsage, "no argument"},
		{"unknown run", &fakeService{frames: frames}, []string{"run", "get", "zzz"}, clientcli.ExitFailed, "zzz"},
		{"refused", &fakeService{frames: frames, dispatch: refuseAll}, []string{"run", "stop", "run-1", "--yes"}, clientcli.ExitFailed, "run not found"},
		{"service unavailable", &fakeService{hello: func() (*pb.HelloResponse, error) {
			return nil, status.Error(codes.Unavailable, "the orchestrator's state directory is held by pid 7")
		}}, []string{"run", "list"}, clientcli.ExitUnavailable, "held by pid 7"},
		{"service without the orchestrator", &fakeService{hello: func() (*pb.HelloResponse, error) {
			return nil, status.Error(codes.Unimplemented, "unknown service")
		}}, []string{"run", "list"}, clientcli.ExitUnavailable, "unavailable"},
		{"service too old", &fakeService{hello: tooOld(pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD)}, []string{"run", "list"}, clientcli.ExitUnavailable, "older than this client needs"},
		{"client too old", &fakeService{hello: tooOld(pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD)}, []string{"run", "list"}, clientcli.ExitFailed, "too old"},
		{"dispatch unavailable", &fakeService{frames: frames, dispatch: func(*pb.Command) (*pb.DispatchResponse, error) {
			return nil, status.Error(codes.Unavailable, "restarting")
		}}, []string{"run", "stop", "run-1", "--yes"}, clientcli.ExitUnavailable, "restarting"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := newWorld(t, serveFake(t, tc.service))
			got := w.run(context.Background(), tc.args...)
			if got.code != tc.code {
				t.Errorf("exit %d, want %d; stdout %q stderr %q", got.code, tc.code, got.stdout, got.stderr)
			}
			contains(t, "stderr", got.stderr, tc.stderr)
		})
	}
}

func TestAHostThatCannotBeFoundIsUnavailable(t *testing.T) {
	w := newWorld(t, nil)
	var out, errb bytes.Buffer
	code := clientcli.Main(context.Background(), []string{"run", "list"}, clientcli.Env{
		Stdout: &out, Stderr: &errb, Dir: w.dir, FS: w.files, Clock: w.clock, IDs: w.ids,
		Connect: func(string) (clientcli.Dial, error) {
			return nil, errors.New("no vibesys-host next to this program or on PATH")
		},
	})
	if code != clientcli.ExitUnavailable {
		t.Fatalf("exit %d, want 4", code)
	}
	contains(t, "stderr", errb.String(), "no vibesys-host")
}

func TestADialThatFailsIsUnavailable(t *testing.T) {
	w := newWorld(t, func(context.Context) (net.Conn, error) { return nil, errors.New("connection refused") })
	got := w.run(context.Background(), "run", "list")
	if got.code != clientcli.ExitUnavailable {
		t.Fatalf("exit %d, want 4; stderr %q", got.code, got.stderr)
	}
}
