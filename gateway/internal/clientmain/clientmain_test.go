package clientmain

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func write(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
}

func noPath(string) (string, error) { return "", errors.New("not found") }

// A client linked into ~/.local/bin starts the vibesys-host beside the file the link points to,
// not one next to the link.
func TestHostBesideTheClientIsFoundThroughASymlink(t *testing.T) {
	dir := t.TempDir()
	release := filepath.Join(dir, "release")
	write(t, filepath.Join(release, "vibesys-client"))
	write(t, filepath.Join(release, "vibesys-host"))
	bin := filepath.Join(dir, "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(bin, "vibesys-client")
	if err := os.Symlink(filepath.Join(release, "vibesys-client"), link); err != nil {
		t.Fatal(err)
	}
	got, err := hostBinary(link, func(string) (string, error) { return "/elsewhere/vibesys-host", nil })
	if err != nil {
		t.Fatal(err)
	}
	// The directory may itself be reached through a link, so compare what both name.
	want, _ := filepath.EvalSymlinks(filepath.Join(release, "vibesys-host"))
	if gotReal, _ := filepath.EvalSymlinks(got); gotReal != want {
		t.Errorf("host = %s, want the one in %s", got, release)
	}
}

func TestWithNoSiblingTheHostOnPathIsUsed(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, "vibesys-client"))
	got, err := hostBinary(filepath.Join(dir, "vibesys-client"), func(name string) (string, error) {
		if name != "vibesys-host" {
			t.Errorf("looked up %q", name)
		}
		return "/usr/local/bin/vibesys-host", nil
	})
	if err != nil || got != "/usr/local/bin/vibesys-host" {
		t.Errorf("host = %q, %v", got, err)
	}
}

func TestWithNoHostAnywhereTheErrorSaysWhereItLooked(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, "vibesys-client"))
	_, err := hostBinary(filepath.Join(dir, "vibesys-client"), noPath)
	if err == nil || !strings.Contains(err.Error(), dir) || !strings.Contains(err.Error(), "PATH") {
		t.Errorf("error = %v, want one naming the directory searched and PATH", err)
	}
}

// A directory named vibesys-host beside the client is not a host.
func TestASiblingThatIsNotAFileIsNotAHost(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, "vibesys-client"))
	if err := os.Mkdir(filepath.Join(dir, "vibesys-host"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := hostBinary(filepath.Join(dir, "vibesys-client"), noPath); err == nil {
		t.Error("a directory was taken for the host")
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
