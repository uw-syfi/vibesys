package production

import (
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// ReleaseExampleSources are the catalogue a host serves and the provisioner that makes repositories
// from its examples, both over the release find locates, with the host's repositories directory (the
// configured one, or ~/.vibesys/repos) as the default parent. The release is looked up at each call:
// a host with none (a developer's build) answers ListCatalogue Unavailable naming the file it lacks,
// and refuses to provision an example, but still makes an empty repository.
func ReleaseExampleSources(find release.Finder, config *pb.HostConfig, home string) (daemon.CatalogueSource, daemon.Provisioner) {
	repositories := hostconfig.RepositoriesDirectory(config, home)
	catalogue := ReleaseCatalogue{Find: find, RepositoriesDirectory: repositories}
	provisioner := &Provisioner{
		Trees: ReleaseExamples{Find: find}, FS: OSProvisionFS{}, Git: ExecGit{Bounds: GitTimeouts{}},
		Default: repositories, Home: home,
	}
	return catalogue, provisioner
}
