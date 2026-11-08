package production

import (
	"context"

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

// Catalogue implements daemon.CatalogueSource. Its entries carry no fit.
func (c ReleaseCatalogue) Catalogue(context.Context) (daemon.Catalogue, error) {
	rel, err := c.Find()
	if err != nil {
		return daemon.Catalogue{}, err
	}
	file, err := rel.Catalogue()
	if err != nil {
		return daemon.Catalogue{}, err
	}
	out := daemon.Catalogue{Release: rel.Version(), RepositoriesDirectory: c.RepositoriesDirectory}
	for _, e := range file.GetExamples() {
		out.Entries = append(out.Entries, &pb.CatalogueEntry{Example: e.GetExample(), Card: e.GetCard(), NeedsClone: e.GetNeedsClone()})
	}
	return out, nil
}

// ReleaseExamples serves the source of an example from the trees the release carries. It is the
// real implementation behind the provisioner's example port: once that port (daemon.ExampleTrees)
// is in this tree, Source returns its ExampleSource and ExampleUnavailable, which are these types
// and errors with the daemon's names.
type ReleaseExamples struct {
	Find release.Finder
}

// Source is the source of the example with the id, or an *release.ExampleNotFoundError when the
// release does not carry it. A failure to find or read the release is returned as it is.
func (e ReleaseExamples) Source(_ context.Context, example string) (release.ExampleSource, error) {
	rel, err := e.Find()
	if err != nil {
		return release.ExampleSource{}, err
	}
	return rel.ExampleSource(example)
}
