package main

import (
	"context"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/uw-syfi/vibesys/orchestrator/release"
	"github.com/uw-syfi/vibesys/orchestrator/release/assemble"
)

// examples lays the repository's example tasks into the release stage and writes the catalogue:
//
//	release-manifest examples VERSION REPO STAGE CLI
//
// REPO is the checkout whose examples/registry.toml lists the examples; STAGE is the release folder
// being built, which gets examples/ and catalogue.pb; CLI is a vibesys-cli this machine can run,
// whose card reader the catalogue is built from. The external examples' files are fetched at the
// revisions the checkout pins, so this step needs the network for them.
func examples(ctx context.Context, args []string) error {
	version := args[0]
	repo, err := filepath.Abs(args[1])
	if err != nil {
		return err
	}
	stage, err := filepath.Abs(args[2])
	if err != nil {
		return err
	}
	cli, err := filepath.Abs(args[3])
	if err != nil {
		return err
	}
	// The card reader's daemon keeps all its state in this folder (a sandbox daemon of its own, so the
	// build neither collides with the builder's daemon nor reaches it), and the folder is short enough
	// to hold a socket.
	work, err := os.MkdirTemp("/tmp", "vsb")
	if err != nil {
		return err
	}
	defer os.RemoveAll(work)

	repoFS := os.DirFS(repo)
	registry, err := fs.ReadFile(repoFS, assemble.RegistryName)
	if err != nil {
		return err
	}
	entries, err := assemble.ParseRegistry(registry)
	if err != nil {
		return err
	}
	scratch := filepath.Join(work, "fetch")
	if err := os.Mkdir(scratch, 0o755); err != nil {
		return err
	}
	staged, err := assemble.Stage(ctx, repoFS, entries, assemble.GitExternals{Repo: repo, Scratch: scratch}, assemble.DirSink{Root: stage})
	if err != nil {
		return err
	}
	cards := assemble.CLICardReader{Program: cli, State: work}
	catalogue, err := assemble.Catalogue(ctx, version, stage, staged, cards)
	if stopErr := cards.Stop(ctx); err == nil && stopErr != nil {
		err = fmt.Errorf("stopping the CLI's daemon: %w", stopErr)
	}
	if err != nil {
		return err
	}
	data, err := release.MarshalCatalogue(catalogue)
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(stage, release.CatalogueName), data, 0o644)
}
