package clientmain

import (
	"context"
	"errors"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

func noPath(string) (string, error) { return "", errors.New("not found") }

func onPath(t *testing.T, found string) func(string) (string, error) {
	return func(name string) (string, error) {
		if name != "vibesys-host" {
			t.Errorf("looked up %q", name)
		}
		return found, nil
	}
}

// installed lays an installed release in a folder reached through a link, as ~/.vibesys/current is,
// and links the client into a bin folder, as the installer does. It returns the link.
func installed(t *testing.T, options ...releasetest.Option) (link, release string) {
	t.Helper()
	dir := t.TempDir()
	release = filepath.Join(dir, "releases", "1.0.0")
	releasetest.Write(t, release, options...)
	if err := os.Symlink(filepath.Join("releases", "1.0.0"), filepath.Join(dir, "current")); err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(dir, "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	link = filepath.Join(bin, "vibesys-client")
	if err := os.Symlink(filepath.Join(dir, "current", "bin", "vibesys-client"), link); err != nil {
		t.Fatal(err)
	}
	return link, release
}

// A client linked into ~/.local/bin starts the host the release's manifest names, in the release
// the link leads to.
func TestHostOfTheReleaseIsFoundThroughSymlinks(t *testing.T) {
	link, release := installed(t)
	got, err := hostBinary(link, onPath(t, "/elsewhere/vibesys-host"))
	if err != nil {
		t.Fatal(err)
	}
	want, _ := filepath.EvalSymlinks(filepath.Join(release, "bin", "vibesys-host"))
	if gotReal, _ := filepath.EvalSymlinks(got); gotReal != want {
		t.Errorf("host = %s, want the one in %s", got, release)
	}
}

func TestWithNoReleaseTheHostOnPathIsUsed(t *testing.T) {
	dir := t.TempDir()
	got, err := hostBinary(filepath.Join(dir, "bin", "vibesys-client"), onPath(t, "/usr/local/bin/vibesys-host"))
	if err != nil || got != "/usr/local/bin/vibesys-host" {
		t.Errorf("host = %q, %v", got, err)
	}
}

func TestWithNoHostAnywhereTheErrorSaysWhereItLooked(t *testing.T) {
	dir := t.TempDir()
	_, err := hostBinary(filepath.Join(dir, "bin", "vibesys-client"), noPath)
	if err == nil || !strings.Contains(err.Error(), filepath.Join(dir, "release.json")) || !strings.Contains(err.Error(), "PATH") {
		t.Errorf("error = %v, want one naming the manifest looked for and PATH", err)
	}
}

// A release whose host is damaged is an error naming the file. The host on PATH is another build,
// and a repaired install must not be hidden by it.
func TestAReleaseWithoutItsHostIsAnErrorAndNotAnotherBuild(t *testing.T) {
	link, _ := installed(t, releasetest.Without("bin/vibesys-host"))
	got, err := hostBinary(link, onPath(t, "/usr/local/bin/vibesys-host"))
	if err == nil || got != "" || !strings.Contains(err.Error(), "bin/vibesys-host") {
		t.Errorf("got %q, %v, want an error naming bin/vibesys-host", got, err)
	}
}

func TestCommandIDsAreDistinct(t *testing.T) {
	seen := map[string]bool{}
	for range 1000 {
		id := randomIDs{}.NewCommandID()
		if seen[id] {
			t.Fatalf("id %s was minted twice", id)
		}
		seen[id] = true
	}
}

// Every signal that ends the command cancels its context, which is what makes a view in key mode put
// the terminal back: Ctrl-C, a plain kill, and the hangup of a closed window alike.
func TestEverySignalThatEndsTheCommandCancelsItsContext(t *testing.T) {
	for _, sig := range []syscall.Signal{syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP} {
		ctx, stop := signal.NotifyContext(context.Background(), stopSignals...)
		if err := syscall.Kill(os.Getpid(), sig); err != nil {
			stop()
			t.Fatal(err)
		}
		select {
		case <-ctx.Done():
		case <-time.After(30 * time.Second): // only a guard against a hang.
			stop()
			t.Fatalf("%v did not cancel the context", sig)
		}
		stop()
	}
}

// Colour is wanted at a terminal unless NO_COLOR holds anything (no-color.org) or TERM says the
// terminal cannot show it.
func TestColourIsWantedUnlessNoColourOrADumbTerminal(t *testing.T) {
	env := func(pairs ...string) func(string) string {
		return func(name string) string {
			for i := 0; i < len(pairs); i += 2 {
				if pairs[i] == name {
					return pairs[i+1]
				}
			}
			return ""
		}
	}
	for _, c := range []struct {
		name string
		env  func(string) string
		want bool
	}{
		{"an ordinary terminal", env("TERM", "xterm-256color"), true},
		{"NO_COLOR set to anything", env("TERM", "xterm", "NO_COLOR", "1"), false},
		{"NO_COLOR set to a word", env("TERM", "xterm", "NO_COLOR", "please"), false},
		{"NO_COLOR set to nothing", env("TERM", "xterm", "NO_COLOR", ""), true},
		{"a dumb terminal", env("TERM", "dumb"), false},
		{"no TERM", env(), false},
	} {
		if got := wantsColor(c.env); got != c.want {
			t.Errorf("%s: wantsColor = %v, want %v", c.name, got, c.want)
		}
	}
}
