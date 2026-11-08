package daemon

import (
	"context"
	"fmt"
	"io/fs"
	"regexp"
	"strings"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// Provisioner makes a repository on the host. A refusal is the response's, not an error; an error is
// a failure of the call to retry, and it leaves nothing behind.
type Provisioner interface {
	Provision(ctx context.Context, req *pb.ProvisionRepositoryRequest) (*pb.ProvisionRepositoryResponse, error)
}

// CloneSpec names a tree that lives in another repository: where it is cloned from and the revision
// it is pinned to.
type CloneSpec struct {
	URL string
	// Revision is the pinned commit. Empty is a registry entry with no pin, which is refused.
	Revision string
}

// ExampleSource is what an example is made of. Vendored paths win: when both are set, the clone is
// laid down first and the vendored files are written over it.
type ExampleSource struct {
	// Vendored is the example's own files, rooted at the example's folder. Nil when it has none.
	Vendored fs.FS
	// Clone is the external repository the rest of the example comes from. Nil when the example is
	// all vendored.
	Clone *CloneSpec
}

// ExampleUnavailableReason says why an example has no source.
type ExampleUnavailableReason string

// The reasons.
const (
	// ExampleNotFound is an example the release does not carry.
	ExampleNotFound ExampleUnavailableReason = "not_found"
	// ExampleUnpinned is an external example whose registry entry records no revision to clone.
	ExampleUnpinned ExampleUnavailableReason = "unpinned"
)

// ExampleUnavailable is the error of an example that has no source; Detail names the example.
type ExampleUnavailable struct {
	Reason ExampleUnavailableReason
	Detail string
}

func (e *ExampleUnavailable) Error() string {
	return fmt.Sprintf("example unavailable (%s): %s", e.Reason, e.Detail)
}

// ExampleTrees serves the source of a catalogue example, by its id. An unknown example, or one that
// cannot be made, is an *ExampleUnavailable; any other error is a failure of the call.
type ExampleTrees interface {
	Source(ctx context.Context, example string) (ExampleSource, error)
}

var repositoryName = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)

// ValidRepositoryName says name is lowercase letters, digits and dashes, from 1 to 63 bytes, and
// does not begin with a dash. It is narrower than the request's own pattern, which also lets dots
// and underscores through; a name is never a path.
func ValidRepositoryName(name string) bool { return repositoryName.MatchString(name) }

// RefuseProvision is a response that refuses a provision for reason.
func RefuseProvision(reason pb.ProvisionRefusal, detail string) *pb.ProvisionRepositoryResponse {
	return &pb.ProvisionRepositoryResponse{Outcome: &pb.ProvisionRepositoryResponse_Refused{
		Refused: &pb.ProvisionRefused{Reason: reason, Detail: detail},
	}}
}

// ProvisionRepository implements OrchestratorService. The name is judged here, so every bad name is
// the typed refusal the screen shows beside its field and not a transport error. A parent that
// begins with "~" is the host's to expand, so the rest of the request is checked with it stood in
// for by the root.
func (s *Service) ProvisionRepository(ctx context.Context, req *pb.ProvisionRepositoryRequest) (*pb.ProvisionRepositoryResponse, error) {
	if s.Provisioner == nil {
		return nil, unimplemented("ProvisionRepository")
	}
	if !ValidRepositoryName(req.GetName()) {
		return RefuseProvision(pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_INVALID,
			fmt.Sprintf("name %q is not made of lowercase letters, digits and dashes, from 1 to 63 of them, beginning with a letter or digit", req.GetName())), nil
	}
	checked := req
	if parent := req.GetParentDirectory(); strings.HasPrefix(parent, "~") {
		checked = proto.Clone(req).(*pb.ProvisionRepositoryRequest)
		checked.ParentDirectory = "/"
	}
	if err := wire.Validate(checked); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	return s.Provisioner.Provision(ctx, req)
}
