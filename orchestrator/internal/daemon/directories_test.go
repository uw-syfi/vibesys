package daemon_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// ListDirectory answers the folder's entries with their marks and the host's truncation, and passes
// the hidden flag on; the names of the tasks of a repository that passes ride on CheckRepository.
func TestListDirectoryAnswersTheListingTheHostGives(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Dirs.Set("~/code", daemon.Listing{
		Entries: []daemon.DirectoryEntry{
			{Name: "app", IsDirectory: true, IsGitRepository: true, HasTasks: true},
			{Name: "notes.txt"},
		},
		Truncated: true,
	})
	got, err := r.Service.ListDirectory(context.Background(), &pb.ListDirectoryRequest{Path: "~/code", IncludeHidden: true})
	if err != nil {
		t.Fatalf("ListDirectory: %v", err)
	}
	want := []*pb.DirectoryEntry{
		{Name: "app", IsDirectory: true, IsGitRepository: true, HasTasks: true},
		{Name: "notes.txt"},
	}
	if len(got.GetEntries()) != len(want) || !got.GetTruncated() {
		t.Fatalf("answered %v, want %v truncated", got, want)
	}
	for i, e := range got.GetEntries() {
		if e.GetName() != want[i].GetName() || e.GetIsDirectory() != want[i].GetIsDirectory() ||
			e.GetIsGitRepository() != want[i].GetIsGitRepository() || e.GetHasTasks() != want[i].GetHasTasks() {
			t.Fatalf("entry %d is %v, want %v", i, e, want[i])
		}
	}
	if calls := r.Dirs.Calls(); len(calls) != 1 || calls[0] != (daemontest.DirectoriesCall{Path: "~/code", Hidden: true}) {
		t.Fatalf("asked %v, want ~/code with hidden", calls)
	}
}

// A folder that cannot be listed is answered with a code that says whose fault it is and the words
// that name the path; a request the contract refuses never reaches the host.
func TestListDirectoryRefusalsNameThePathAndTheCode(t *testing.T) {
	cases := []struct {
		fault daemon.DirectoryFault
		code  codes.Code
	}{
		{daemon.DirectoryNotAbsolute, codes.InvalidArgument},
		{daemon.DirectoryNotFound, codes.NotFound},
		{daemon.DirectoryNotADirectory, codes.InvalidArgument},
		{daemon.DirectoryDenied, codes.PermissionDenied},
	}
	for _, c := range cases {
		r := daemontest.NewRig(t)
		r.Dirs.Fail("/x", &daemon.DirectoryError{Fault: c.fault, Reason: "path /x is wrong"})
		_, err := r.Service.ListDirectory(context.Background(), &pb.ListDirectoryRequest{Path: "/x"})
		if status.Code(err) != c.code || !strings.Contains(err.Error(), "/x") {
			t.Fatalf("fault %d gave %v, want %v naming /x", c.fault, err, c.code)
		}
	}
	r := daemontest.NewRig(t)
	r.Dirs.Fail("/x", errors.New("disk on fire"))
	if _, err := r.Service.ListDirectory(context.Background(), &pb.ListDirectoryRequest{Path: "/x"}); status.Code(err) != codes.Unavailable {
		t.Fatalf("an unexpected failure gave %v, want Unavailable", err)
	}
	if _, err := r.Service.ListDirectory(context.Background(), &pb.ListDirectoryRequest{}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("an empty path gave %v, want InvalidArgument", err)
	}
	if n := len(r.Dirs.Calls()); n != 1 {
		t.Fatalf("%d calls reached the host, want only the one that passed validation", n)
	}
}

// An agent may not list the host's folders.
func TestAnAgentMayNotListADirectory(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startRun("c1"))
	r.Settle(t)
	run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	lead, _ := r.Daemon.State().Agent(run.Lead)
	ctx := agentContext(lead.ID, lead.Session, r.Auth.Token(core.Session{Agent: lead.ID, N: lead.Session}))
	if _, err := r.AgentService.ListDirectory(ctx, &pb.ListDirectoryRequest{Path: "/"}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("an agent's ListDirectory gave %v, want PermissionDenied", err)
	}
}

// A repository that passes the check comes back with its tasks, and one that fails with none.
func TestCheckRepositoryNamesTheTasksOfAPassingRepository(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Starts.SetTasks("/repo", "alpha", "beta")
	got, err := r.Service.CheckRepository(context.Background(), &pb.CheckRepositoryRequest{Repository: "/repo"})
	if err != nil || strings.Join(got.GetTasks(), ",") != "alpha,beta" {
		t.Fatalf("got %v, %v, want tasks alpha and beta", got, err)
	}
	r.Starts.Refuse(&daemon.StartRefusal{Reason: "repository /repo is not a git work tree"})
	got, err = r.Service.CheckRepository(context.Background(), &pb.CheckRepositoryRequest{Repository: "/repo"})
	if err != nil || len(got.GetTasks()) != 0 {
		t.Fatalf("got %v, %v, want no tasks for a refused repository", got, err)
	}
}
