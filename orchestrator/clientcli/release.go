package clientcli

import (
	"fmt"
	"path/filepath"
	"runtime"

	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// releaseVerify is `release verify [--installed] DIR`: it checks the release in DIR against its own
// manifest and prints its version, so the installer needs only this exit status to know a bundle is
// whole and runs here. Without --installed it checks the archive as the builder packs it; with it,
// the release after install has made the session holder's environment. It needs no host.
func releaseVerify(c *call, args []string) error {
	fs := flags("release verify")
	installed := fs.Bool("installed", false, "also require what install makes")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	if len(positional) != 1 {
		return usageError("release verify takes the release's folder")
	}
	dir := positional[0]
	if !filepath.IsAbs(dir) {
		dir = filepath.Join(c.Dir, dir)
	}
	rel, err := release.Open(filepath.Clean(dir), c.FS)
	if err != nil {
		return failed("%v", err)
	}
	stage := release.Archived
	if *installed {
		stage = release.Installed
	}
	if err := rel.Verify(stage); err != nil {
		return failed("%v", err)
	}
	if err := rel.CheckPlatform(runtime.GOOS, runtime.GOARCH); err != nil {
		return failed("%v", err)
	}
	_, err = fmt.Fprintln(c.Stdout, rel.Version())
	return err
}
