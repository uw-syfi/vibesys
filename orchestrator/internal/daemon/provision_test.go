package daemon_test

import (
	"context"
	"math/rand/v2"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

func provisionService(t *testing.T, p daemon.Provisioner) *daemon.Service {
	t.Helper()
	rig := daemontest.NewRig(t)
	svc, err := daemon.NewService(rig.Daemon.Engine, daemon.ServiceDeps{
		Authority: rig.Auth, Audience: daemon.AudienceOperator, Bounds: rig.Bounds, Binary: "test", Starts: rig.Starts, Provisioner: p,
	})
	if err != nil {
		t.Fatal(err)
	}
	return svc
}

func emptyRequest(name, parent string) *pb.ProvisionRepositoryRequest {
	return &pb.ProvisionRepositoryRequest{
		RequestId: "r1", Name: name, ParentDirectory: parent,
		Source: &pb.ProvisionRepositoryRequest_Empty{Empty: &pb.EmptyRepository{}},
	}
}

// Every name outside lowercase letters, digits and dashes is the typed refusal the screen shows
// beside its field, never a transport error, and never reaches the provisioner. A good name does.
func TestProvisionRepositoryJudgesTheNameBeforeTheProvisioner(t *testing.T) {
	alphabet := []rune("abcxyz019-_./~ ABC\x00é")
	r := rand.New(rand.NewPCG(7, 7))
	var fake daemontest.FakeProvisioner
	svc := provisionService(t, &fake)
	for range 400 {
		name := make([]rune, r.IntN(70))
		for i := range name {
			name[i] = alphabet[r.IntN(len(alphabet))]
		}
		got, err := svc.ProvisionRepository(context.Background(), emptyRequest(string(name), ""))
		if daemon.ValidRepositoryName(string(name)) {
			if err != nil || got.GetProvisioned() == nil {
				t.Fatalf("name %q: %v, %v, want it provisioned", name, got, err)
			}
			continue
		}
		if err != nil || got.GetRefused().GetReason() != pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_INVALID {
			t.Fatalf("name %q: %v, %v, want the name refused as invalid", name, got, err)
		}
	}
	for _, call := range fake.Calls() {
		if !daemon.ValidRepositoryName(call.GetName()) {
			t.Fatalf("the provisioner was asked for %q", call.GetName())
		}
	}
}

// A parent that begins with ~ is passed on as it is, for the host to expand; a relative one is a
// malformed request.
func TestProvisionRepositoryLeavesTildeToTheProvisioner(t *testing.T) {
	var fake daemontest.FakeProvisioner
	svc := provisionService(t, &fake)
	if _, err := svc.ProvisionRepository(context.Background(), emptyRequest("mine", "~/work")); err != nil {
		t.Fatal(err)
	}
	if calls := fake.Calls(); len(calls) != 1 || calls[0].GetParentDirectory() != "~/work" {
		t.Fatalf("calls %v, want the parent as sent", calls)
	}
	for _, parent := range []string{"relative/dir", "~someone/work"} {
		_, err := svc.ProvisionRepository(context.Background(), emptyRequest("mine", parent))
		if status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), "parent_directory") {
			t.Fatalf("parent %q: %v, want InvalidArgument naming the field", parent, err)
		}
	}
}
