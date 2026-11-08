package assemble

import (
	"bytes"
	"context"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// DirSink is a Sink that writes under a folder on this machine.
type DirSink struct{ Root string }

var _ Sink = DirSink{}

// MkdirAll implements Sink.
func (s DirSink) MkdirAll(dir string) error {
	return os.MkdirAll(filepath.Join(s.Root, filepath.FromSlash(dir)), 0o755)
}

// WriteFile implements Sink.
func (s DirSink) WriteFile(name string, data []byte, perm fs.FileMode) error {
	return os.WriteFile(filepath.Join(s.Root, filepath.FromSlash(name)), data, perm)
}

// CLICardReader reads a card by running `vibesys-cli task card --json --folder`. The CLI's daemon is
// the one reader of task files, so this is the reader a run would use. Env is the environment the
// program runs in, so a builder gives it a state folder of its own, and the daemon it starts is
// stopped with Stop.
type CLICardReader struct {
	Program string
	Env     []string
}

var _ CardReader = CLICardReader{}

// Card implements CardReader.
func (r CLICardReader) Card(ctx context.Context, folder, task string) (*cliv1.TaskCard, error) {
	out, err := r.run(ctx, "task", "card", "--json", "--folder", folder, task)
	if err != nil {
		return nil, err
	}
	var card cliv1.TaskCard
	if err := (protojson.UnmarshalOptions{}).Unmarshal(out, &card); err != nil {
		return nil, fmt.Errorf("the card the CLI printed is not a TaskCard: %w", err)
	}
	return &card, nil
}

// Stop stops the daemon the card reads started.
func (r CLICardReader) Stop(ctx context.Context) error {
	_, err := r.run(ctx, "daemon", "stop")
	return err
}

func (r CLICardReader) run(ctx context.Context, args ...string) ([]byte, error) {
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, r.Program, args...)
	cmd.Env, cmd.Stdout, cmd.Stderr = r.Env, &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("%s %s: %w: %s", filepath.Base(r.Program), strings.Join(args[:2], " "), err, strings.TrimSpace(stderr.String()))
	}
	return stdout.Bytes(), nil
}

// GitExternals fetches an external example's own files from the repository the checkout pins as a
// submodule: the URL from .gitmodules and the commit from the gitlink, at the checkout's HEAD.
// Scratch is an empty folder the fetches go in.
type GitExternals struct {
	Repo    string
	Scratch string
}

var _ Externals = GitExternals{}

// Fetch implements Externals. It fetches the one commit, without history or file contents but the
// example's .vibesys folder.
func (g GitExternals) Fetch(ctx context.Context, e Entry) (fs.FS, Pin, error) {
	urlOut, err := git(ctx, g.Repo, "config", "--file", ".gitmodules", "--get-regexp", `^submodule\..*\.(path|url)$`)
	if err != nil {
		return nil, Pin{}, err
	}
	url, err := SubmoduleURL(urlOut, e.Path)
	if err != nil {
		return nil, Pin{}, err
	}
	tree, err := git(ctx, g.Repo, "ls-tree", "HEAD", "--", e.Path)
	if err != nil {
		return nil, Pin{}, err
	}
	commit, err := Gitlink(tree, e.Path)
	if err != nil {
		return nil, Pin{}, err
	}
	dir := filepath.Join(g.Scratch, strings.ReplaceAll(e.ID(), "/", "_"))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, Pin{}, err
	}
	for _, args := range [][]string{
		{"init", "--quiet"},
		{"remote", "add", "origin", url},
		{"sparse-checkout", "init", "--cone"},
		{"sparse-checkout", "set", ".vibesys"},
		{"fetch", "--quiet", "--depth", "1", "--filter=blob:none", "origin", commit},
		{"checkout", "--quiet", "--detach", commit},
	} {
		if _, err := git(ctx, dir, args...); err != nil {
			return nil, Pin{}, err
		}
	}
	return os.DirFS(dir), Pin{URL: url, Revision: commit}, nil
}

func git(ctx context.Context, dir string, args ...string) (string, error) {
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", dir}, args...)...)
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("git %s: %w: %s", args[0], err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}
