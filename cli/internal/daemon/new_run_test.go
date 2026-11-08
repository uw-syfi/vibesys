package daemon_test

import (
	"context"
	"net"
	"strings"
	"testing"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// describeRig is a daemon over memory card files, reached through an in-memory connection.
type describeRig struct {
	client   cliv1.DaemonServiceClient
	files    *task.MemoryCardFiles
	branches *gitrepo.FakeBranches
	stores   *memstores.Stores
	tasks    *task.MemorySource
}

func newDescribeRig(t *testing.T, withFiles bool) *describeRig {
	t.Helper()
	env := fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates())
	envs := fakeenv.Environments{core.EnvironmentHost: env}
	survivors := supervisor.NewSurvivors()
	files, branches, stores, tasks := task.NewMemoryCardFiles(), gitrepo.NewFakeBranches(), memstores.New(app.Rules(5)), task.NewMemorySource()
	a := app.New(app.Config{
		Clock: clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond), Stores: stores, Branches: branches, Tasks: tasks,
		Environments: envs, Supervisors: supervisor.NewInProcess(envs, &fakeenv.Checkouts{}, survivors), Repositories: repos(),
	})
	if err := a.Start(ctxFor(t)); err != nil {
		t.Fatal(err)
	}
	srv := daemon.NewServer(a, "test", daemon.Inherited{})
	if withFiles {
		srv.WithCardFiles(files)
	}
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
	})
	return &describeRig{client: cliv1.NewDaemonServiceClient(conn), files: files, branches: branches, stores: stores, tasks: tasks}
}

const describedManifest = `version = "0.2"
[catalogue]
domain = "databases"
[catalogue.hardware]
accelerator = "CPU"
count = 8
vendor = "cpu"
`

func taskTree(manifest string) task.Tree {
	return task.Tree{
		".vibesys/tasks/t/" + task.ManifestName:  {Data: []byte(manifest)},
		".vibesys/tasks/t/" + task.ObjectiveName: {Data: []byte("[[objective]]\nname = \"qps\"\ndirection = \"max\"\n")},
		".vibesys/tasks/t/OBJECTIVE.md":          {Data: []byte("# Fast KV\n")},
	}
}

func sources() map[string]*cliv1.DescribeTaskRequest {
	return map[string]*cliv1.DescribeTaskRequest{
		"committed": {Source: &cliv1.DescribeTaskRequest_Committed{Committed: &cliv1.CommittedTask{Repository: "/repo", Task: "t"}}},
		"revision": {Source: &cliv1.DescribeTaskRequest_Committed{Committed: &cliv1.CommittedTask{
			Repository: "/repo", Revision: ptr("v1"), Task: "t"}}},
		"folder":       {Source: &cliv1.DescribeTaskRequest_Folder{Folder: &cliv1.FolderTask{Root: "/repo", Task: "t"}}},
		"working tree": {Source: &cliv1.DescribeTaskRequest_WorkingTree{WorkingTree: &cliv1.WorkingTreeTask{Repository: "/repo", Task: "t"}}},
	}
}

func ptr(s string) *string { return &s }

func (r *describeRig) put(manifest string) {
	r.files.PutCommit("/repo", "HEAD", taskTree(manifest))
	r.files.PutCommit("/repo", "v1", taskTree(manifest))
	r.files.PutFolder("/repo", taskTree(manifest))
	r.files.PutWorkingTree("/repo", taskTree(manifest))
}

// Whichever way the task is reached, the same files give the same card, and the card meets its rules.
func TestDescribeTaskAnswersTheCardFromEverySource(t *testing.T) {
	r := newDescribeRig(t, true)
	r.put(describedManifest)
	for name, req := range sources() {
		resp, err := r.client.DescribeTask(ctxFor(t), req)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		card := resp.GetCard()
		if card == nil || card.GetTask() != "t" || card.GetHeading() != "Fast KV" || card.GetHardware().GetCount() != 8 ||
			card.GetHardware().GetVendor() != cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_CPU || card.GetDomain() != cliv1.TaskDomain_TASK_DOMAIN_DATABASES ||
			len(card.GetObjectives()) != 1 {
			t.Errorf("%s: card %v", name, resp)
		}
		if err := protovalidate.Validate(resp); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// A task that is not there, or a place that is not, is NOT_FOUND; an invalid file is INVALID with the
// key in the detail. Both are answers, not errors, and neither carries a card.
func TestDescribeTaskSaysWhyATaskHasNoCard(t *testing.T) {
	r := newDescribeRig(t, true)
	r.put(strings.Replace(describedManifest, "count = 8\n", "count = 8\nvariant = \"x\"\n", 1))
	for name, req := range sources() {
		resp, err := r.client.DescribeTask(ctxFor(t), req)
		u := resp.GetUnreadable()
		if err != nil || resp.GetCard() != nil || u.GetReason() != cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_INVALID ||
			!strings.Contains(u.GetDetail(), "variant") || !strings.Contains(u.GetDetail(), task.ManifestName) {
			t.Errorf("%s: %v, %v; want INVALID naming the file and key", name, resp, err)
		}
	}
	r.put(describedManifest)
	missing := &cliv1.DescribeTaskRequest{Source: &cliv1.DescribeTaskRequest_Committed{Committed: &cliv1.CommittedTask{Repository: "/repo", Task: "absent"}}}
	elsewhere := &cliv1.DescribeTaskRequest{Source: &cliv1.DescribeTaskRequest_Folder{Folder: &cliv1.FolderTask{Root: "/nowhere", Task: "t"}}}
	for name, req := range map[string]*cliv1.DescribeTaskRequest{"task": missing, "folder": elsewhere} {
		resp, err := r.client.DescribeTask(ctxFor(t), req)
		if err != nil || resp.GetUnreadable().GetReason() != cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_NOT_FOUND {
			t.Errorf("%s: %v, %v; want NOT_FOUND", name, resp, err)
		}
	}
}

// A request that breaks its rules, and a daemon with no reader, are errors a client cannot mistake
// for a task with no card.
func TestDescribeTaskRefusesWhatItCannotAnswer(t *testing.T) {
	r := newDescribeRig(t, true)
	for name, req := range map[string]*cliv1.DescribeTaskRequest{
		"empty":    {},
		"relative": {Source: &cliv1.DescribeTaskRequest_Folder{Folder: &cliv1.FolderTask{Root: "relative/dir", Task: "t"}}},
		"no name":  {Source: &cliv1.DescribeTaskRequest_Folder{Folder: &cliv1.FolderTask{Root: "/repo"}}},
	} {
		if _, err := r.client.DescribeTask(ctxFor(t), req); status.Code(err) != codes.InvalidArgument {
			t.Errorf("%s: got %v, want InvalidArgument", name, err)
		}
	}
	bare := newDescribeRig(t, false)
	if _, err := bare.client.DescribeTask(ctxFor(t), sources()["folder"]); status.Code(err) != codes.Unavailable {
		t.Errorf("no reader: got %v, want Unavailable", err)
	}
}
