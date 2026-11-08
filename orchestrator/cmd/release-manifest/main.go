// release-manifest writes RELEASE/release.json for a release the builder has laid out, after
// checking that every file the manifest requires is there:
//
//	release-manifest VERSION OS ARCH DIR
//
// And, before the manifest, the example tasks the release carries:
//
//	release-manifest examples VERSION REPO STAGE CLI
//
// which lays them into STAGE and writes catalogue.pb (see examples.go).
//
// It is the builder's only way to write the manifest, so the layout it records is the one in
// package release and no shell script restates it. It fails naming each missing file.
package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/uw-syfi/vibesys/orchestrator/release"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "release-manifest: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 5 && args[0] == "examples" {
		return examples(context.Background(), args[1:])
	}
	if len(args) != 4 {
		fmt.Fprintln(os.Stderr, "usage: release-manifest VERSION OS ARCH DIR | release-manifest examples VERSION REPO STAGE CLI")
		os.Exit(2)
	}
	version, goos, goarch := args[0], args[1], args[2]
	dir, err := filepath.Abs(args[3])
	if err != nil {
		return err
	}
	m, err := release.NewManifest(version, goos, goarch)
	if err != nil {
		return err
	}
	if err := release.New(dir, m, os.DirFS("/")).Verify(release.Archived); err != nil {
		return err
	}
	data, err := release.Marshal(m)
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, release.ManifestName), data, 0o644)
}
