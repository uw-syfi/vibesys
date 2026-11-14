package production

import (
	"context"
	"errors"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// ReleaseCatalogue is the daemon.CatalogueSource over the release the host runs: the example tasks
// its catalogue.pb lists, which the release builder made from the task files. The release is looked
// up at each call, so a repaired install is seen at once.
type ReleaseCatalogue struct {
	Find release.Finder
	// RepositoriesDirectory is the host's default directory for provisioned repositories.
	RepositoriesDirectory string
}

var _ daemon.CatalogueSource = ReleaseCatalogue{}

// Catalogue implements daemon.CatalogueSource. Its entries carry no fit. RepositoriesDirectory is
// set even on a failure, so a caller that turns the error into a classified problem still has it.
func (c ReleaseCatalogue) Catalogue(context.Context) (daemon.Catalogue, error) {
	rel, err := c.Find()
	if err != nil {
		return daemon.Catalogue{RepositoriesDirectory: c.RepositoriesDirectory}, err
	}
	file, err := rel.Catalogue()
	if err != nil {
		return daemon.Catalogue{RepositoriesDirectory: c.RepositoriesDirectory}, err
	}
	out := daemon.Catalogue{Release: rel.Version(), RepositoriesDirectory: c.RepositoriesDirectory}
	for _, e := range file.GetExamples() {
		out.Entries = append(out.Entries, &pb.CatalogueEntry{Example: e.GetExample(), Card: e.GetCard(), NeedsClone: e.GetNeedsClone()})
	}
	return out, nil
}

// Problem implements daemon.CatalogueSource: it classifies the two causes a client can act on (no
// release at all, or one missing files), the same release errors problemReason reads for the parts of
// HostControlService.Identify. Any other failure (a corrupt catalogue.pb, a places read that cannot
// run) is nil, so ListCatalogue keeps answering Unavailable for it.
func (ReleaseCatalogue) Problem(err error) *pb.CatalogueProblem {
	var notInstalled *release.NotInstalledError
	var incomplete *release.IncompleteError
	switch {
	case errors.As(err, &notInstalled):
		return &pb.CatalogueProblem{Reason: pb.CatalogueProblemReason_CATALOGUE_PROBLEM_REASON_NO_RELEASE, Detail: err.Error()}
	case errors.As(err, &incomplete):
		return &pb.CatalogueProblem{Reason: pb.CatalogueProblemReason_CATALOGUE_PROBLEM_REASON_RELEASE_INCOMPLETE, Detail: err.Error()}
	default:
		return nil
	}
}

// ReleaseExamples serves the source of an example from the trees the release carries. It is the
// real implementation behind the provisioner's example port, daemon.ExampleTrees.
type ReleaseExamples struct {
	Find release.Finder
}

var _ daemon.ExampleTrees = ReleaseExamples{}

// Source is the source of the example with the id. An example the release does not carry, or an
// external one the registry records no pin for, is a *daemon.ExampleUnavailable; a failure to find or
// read the release is returned as it is.
func (e ReleaseExamples) Source(_ context.Context, example string) (daemon.ExampleSource, error) {
	rel, err := e.Find()
	if err != nil {
		return daemon.ExampleSource{}, err
	}
	src, err := rel.ExampleSource(example)
	var missing *release.ExampleNotFoundError
	if errors.As(err, &missing) {
		return daemon.ExampleSource{}, &daemon.ExampleUnavailable{Reason: daemon.ExampleNotFound, Detail: missing.Example}
	}
	if err != nil {
		return daemon.ExampleSource{}, err
	}
	out := daemon.ExampleSource{Vendored: src.Vendored}
	if src.Clone != nil {
		if src.Clone.Revision == "" {
			return daemon.ExampleSource{}, &daemon.ExampleUnavailable{Reason: daemon.ExampleUnpinned, Detail: example}
		}
		out.Clone = &daemon.CloneSpec{URL: src.Clone.URL, Revision: src.Clone.Revision}
	}
	return out, nil
}
