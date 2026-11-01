package daemon_test

import (
	"context"
	"io"
	"net"
	"os"
	"strings"
	"testing"
	"testing/quick"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// backstop only keeps a hung test from hanging the build; no test passes or fails by how long it
// took.
const backstop = time.Minute

var (
	commit = core.Commit("0123456789abcdef0123456789abcdef01234567")
	call   = &cliv1.CallContext{Repository: "/repo", Task: "task", RunId: "run-1", Harness: "h", Agent: "agent-1"}
)

func ctxFor(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), backstop)
	t.Cleanup(cancel)
	return ctx
}

// rig is a daemon server over fakes, reached through an in-memory connection.
type rig struct {
	t      *testing.T
	gates  *fakeenv.MemoryGates
	server *daemon.Server
	client cliv1.DaemonServiceClient
}

func newRig(t *testing.T) *rig {
	t.Helper()
	gates := fakeenv.NewMemoryGates()
	env := fakeenv.New(core.EnvironmentHost, gates)
	checkouts := &fakeenv.Checkouts{}
	survivors := supervisor.NewSurvivors()
	a := app.New(app.Config{
		Clock:        clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond),
		Stores:       app.NewMemoryStores(app.Rules(5)),
		Environments: fakeenv.Environments{core.EnvironmentHost: env},
		Supervisors:  supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: env}, checkouts, survivors),
	})
	if err := a.Start(ctxFor(t)); err != nil {
		t.Fatal(err)
	}
	srv := daemon.NewServer(a, "test")
	listener := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	cliv1.RegisterDaemonServiceServer(gs, srv)
	go gs.Serve(listener)
	conn, err := grpc.NewClient("passthrough:///daemon",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		conn.Close()
		srv.Shutdown()
		gs.Stop()
		a.Close()
		survivors.Close()
		if n := env.Active(); n != 0 {
			t.Errorf("%d commands still run", n)
		}
	})
	return &rig{t: t, gates: gates, server: srv, client: cliv1.NewDaemonServiceClient(conn)}
}

func spec(argv ...string) *cliv1.JobSpec {
	return wire.JobSpec(core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Argv: argv}})
}

func (r *rig) submit(argv ...string) *cliv1.Job {
	r.t.Helper()
	resp, err := r.client.Submit(ctxFor(r.t), &cliv1.SubmitRequest{Context: call, Spec: spec(argv...)})
	if err != nil {
		r.t.Fatalf("Submit: %v", err)
	}
	return resp.GetJob()
}

// final watches the jobs until all are final and returns the last state of each.
func (r *rig) final(ids ...string) map[string]*cliv1.Job {
	r.t.Helper()
	stream, err := r.client.Watch(ctxFor(r.t), &cliv1.WatchRequest{Context: call, JobIds: ids})
	if err != nil {
		r.t.Fatalf("Watch: %v", err)
	}
	last := map[string]*cliv1.Job{}
	for {
		resp, err := stream.Recv()
		if err == io.EOF {
			return last
		}
		if err != nil {
			r.t.Fatalf("Watch: %v", err)
		}
		last[resp.GetJob().GetId()] = resp.GetJob()
	}
}

func (r *rig) logs(id string) string {
	r.t.Helper()
	stream, err := r.client.ReadLogs(ctxFor(r.t), &cliv1.ReadLogsRequest{Context: call, JobId: id})
	if err != nil {
		r.t.Fatal(err)
	}
	var out strings.Builder
	for {
		resp, err := stream.Recv()
		if err == io.EOF {
			return out.String()
		}
		if err != nil {
			r.t.Fatal(err)
		}
		out.Write(resp.GetData())
	}
}

func TestASubmittedJobRunsAndItsOutputIsRead(t *testing.T) {
	r := newRig(t)
	job := r.submit("print", "hello", ";", "exit", "0")
	if job.GetState() != cliv1.JobState_JOB_STATE_QUEUED && job.GetState() != cliv1.JobState_JOB_STATE_RUNNING {
		t.Fatalf("a submitted job is %s", job.GetState())
	}
	got := r.final(job.GetId())[job.GetId()]
	if got.GetState() != cliv1.JobState_JOB_STATE_SUCCEEDED {
		t.Fatalf("job ended %s", got.GetState())
	}
	if out := r.logs(job.GetId()); out != "hello\n" {
		t.Fatalf("output = %q, want %q", out, "hello\n")
	}
	list, err := r.client.ListJobs(ctxFor(t), &cliv1.ListJobsRequest{Context: call})
	if err != nil || len(list.GetJobs()) != 1 || list.GetJobs()[0].GetId() != job.GetId() {
		t.Fatalf("ListJobs = %v, %v", list.GetJobs(), err)
	}
}

func TestACancelEndsARunningJobAndNamesTheReason(t *testing.T) {
	r := newRig(t)
	job := r.submit("print", "started", ";", "hold", "g", ";", "exit", "0")
	resp, err := r.client.Cancel(ctxFor(t), &cliv1.CancelRequest{Context: call, JobId: job.GetId(), Reason: "no longer needed"})
	if err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	if resp.GetJob().GetState() != cliv1.JobState_JOB_STATE_CANCELLED {
		t.Fatalf("job is %s after cancel", resp.GetJob().GetState())
	}
	if !strings.Contains(resp.GetJob().GetCancelReason(), "no longer needed") {
		t.Fatalf("cancel reason = %q, want the reason given", resp.GetJob().GetCancelReason())
	}
}

func TestANoteIsAddedToAJob(t *testing.T) {
	r := newRig(t)
	job := r.submit("exit", "0")
	r.final(job.GetId())
	resp, err := r.client.AddNote(ctxFor(t), &cliv1.AddNoteRequest{Context: call, JobId: job.GetId(), Text: "looked at it"})
	if err != nil {
		t.Fatal(err)
	}
	if n := resp.GetJob().GetNotes(); len(n) != 1 || n[0].GetText() != "looked at it" {
		t.Fatalf("notes = %v", n)
	}
}

// A call that cannot complete carries a Failure that names what was wrong, whatever the call.
func TestAFailedCallCarriesAFailure(t *testing.T) {
	r := newRig(t)
	_, err := r.client.AddNote(ctxFor(t), &cliv1.AddNoteRequest{Context: call, JobId: "j999", Text: "x"})
	f, ok := wire.FailureOf(err)
	if !ok || f.Kind != core.FailureNotFound {
		t.Fatalf("unknown job: failure = %+v ok %v, want not_found", f, ok)
	}
	_, err = r.client.Submit(ctxFor(t), &cliv1.SubmitRequest{Context: call, Spec: nil})
	if f, ok := wire.FailureOf(err); !ok || f.Kind != core.FailureInvalidInput {
		t.Fatalf("no spec: failure = %+v ok %v, want invalid_input", f, ok)
	}
	// A stream reports its failure on the first receive.
	stream, err := r.client.Watch(ctxFor(t), &cliv1.WatchRequest{Context: call})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = stream.Recv(); err == nil {
		t.Fatal("a watch of no jobs was accepted")
	}
	if f, ok := wire.FailureOf(err); !ok || f.Kind != core.FailureInvalidInput {
		t.Fatalf("empty watch: failure = %+v ok %v, want invalid_input", f, ok)
	}
}

// The calls that arrive with later work are refused as unavailable, each by name.
func TestCallsOfLaterWorkAreUnavailable(t *testing.T) {
	r := newRig(t)
	ctx := ctxFor(t)
	calls := map[string]func() error{
		"Show":          func() error { _, err := r.client.Show(ctx, &cliv1.ShowRequest{}); return err },
		"Compare":       func() error { _, err := r.client.Compare(ctx, &cliv1.CompareRequest{}); return err },
		"Promote":       func() error { _, err := r.client.Promote(ctx, &cliv1.PromoteRequest{}); return err },
		"CurrentResult": func() error { _, err := r.client.CurrentResult(ctx, &cliv1.CurrentResultRequest{}); return err },
		"CreatePlan":    func() error { _, err := r.client.CreatePlan(ctx, &cliv1.CreatePlanRequest{}); return err },
		"ListPlans":     func() error { _, err := r.client.ListPlans(ctx, &cliv1.ListPlansRequest{}); return err },
		"ViewPlan":      func() error { _, err := r.client.ViewPlan(ctx, &cliv1.ViewPlanRequest{}); return err },
		"EditPlan":      func() error { _, err := r.client.EditPlan(ctx, &cliv1.EditPlanRequest{}); return err },
		"CommentPlan":   func() error { _, err := r.client.CommentPlan(ctx, &cliv1.CommentPlanRequest{}); return err },
		"ClosePlan":     func() error { _, err := r.client.ClosePlan(ctx, &cliv1.ClosePlanRequest{}); return err },
	}
	for name, call := range calls {
		f, ok := wire.FailureOf(call())
		if !ok || f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, name) {
			t.Errorf("%s: failure = %+v ok %v, want unavailable naming the call", name, f, ok)
		}
	}
}

// Hello answers what core says of the two builds' versions, for any client versions.
func TestHelloAnswersWhatCoreSays(t *testing.T) {
	r := newRig(t)
	ours := core.CurrentVersions()
	property := func(protocol, minProtocol, schema uint32) bool {
		theirs := core.Versions{Protocol: protocol, MinProtocol: minProtocol, Schema: schema}
		resp, err := r.client.Hello(ctxFor(t), &cliv1.HelloRequest{
			Component: cliv1.Component_COMPONENT_CLI, Versions: wire.BuildVersions("test", theirs)})
		return err == nil && wire.ToCompatibility(resp.GetCompatibility()) == core.CheckCompatibility(theirs, ours)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

func TestHelloRefusesAnotherComponent(t *testing.T) {
	r := newRig(t)
	_, err := r.client.Hello(ctxFor(t), &cliv1.HelloRequest{Component: cliv1.Component_COMPONENT_SUPERVISOR})
	if f, ok := wire.FailureOf(err); !ok || f.Kind != core.FailureInvalidInput {
		t.Fatalf("failure = %+v ok %v, want invalid_input", f, ok)
	}
}

func TestStatusCountsTheQueue(t *testing.T) {
	r := newRig(t)
	job := r.submit("hold", "g", ";", "exit", "0")
	// The job starts running; the gate keeps it so.
	stream, err := r.client.Watch(ctxFor(t), &cliv1.WatchRequest{Context: call, JobIds: []string{job.GetId()}})
	if err != nil {
		t.Fatal(err)
	}
	for {
		resp, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if resp.GetJob().GetState() == cliv1.JobState_JOB_STATE_RUNNING {
			break
		}
	}
	st, err := r.client.Status(ctxFor(t), &cliv1.StatusRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if st.GetRunningJobs() != 1 || st.GetQueuedJobs() != 0 || st.GetPid() != uint32(os.Getpid()) {
		t.Fatalf("status = running %d queued %d pid %d", st.GetRunningJobs(), st.GetQueuedJobs(), st.GetPid())
	}
	r.gates.Release("g")
	r.final(job.GetId())
}

// Stop ends a call that waits on a job, and Stopping is closed once it is asked.
func TestStopEndsWaitingCallsAndSignalsTheProcess(t *testing.T) {
	r := newRig(t)
	job := r.submit("hold", "g", ";", "exit", "0")
	stream, err := r.client.Watch(ctxFor(t), &cliv1.WatchRequest{Context: call, JobIds: []string{job.GetId()}})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-r.server.Stopping():
		t.Fatal("Stopping is closed before Stop")
	default:
	}
	if _, err := r.client.Stop(ctxFor(t), &cliv1.StopRequest{}); err != nil {
		t.Fatal(err)
	}
	<-r.server.Stopping()
	for {
		if _, err := stream.Recv(); err != nil {
			break
		}
	}
	r.gates.Release("g")
}
