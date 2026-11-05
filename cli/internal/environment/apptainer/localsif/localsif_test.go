//go:build linux || darwin

package localsif_test

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/localsif"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

// fakeApptainer stands for the machine's apptainer: version prints one, and build checks that its
// temporary directories are the build's own, writes a file in each, and makes a SIF of the
// archive, or fails as apptainer does on what is not one.
const fakeApptainer = `#!/bin/sh
if [ "$1" = version ]; then echo 1.4.5-fake; exit 0; fi
[ "$1" = build ] && [ "$2" = --force ] || exit 64
out="$3"; src="${4#docker-archive:}"
case "$APPTAINER_TMPDIR" in "$(dirname "$out")"/tmp) ;; *) echo "FATAL: tmp $APPTAINER_TMPDIR" >&2; exit 255 ;; esac
touch "$APPTAINER_TMPDIR/layer" "$APPTAINER_CACHEDIR/blob"
echo "INFO: building" >&2
grep -q '^a docker archive$' "$src" || { echo "FATAL: not a docker archive" >&2; exit 255; }
{ printf 'SIF of '; cat "$src"; } > "$out"
`

// onPath puts a fake apptainer first on PATH and returns the converter that finds it.
func onPath(t *testing.T) *localsif.Converter {
	t.Helper()
	bin := t.TempDir()
	if err := os.WriteFile(filepath.Join(bin, "apptainer"), []byte(fakeApptainer), 0o755); err != nil {
		t.Fatal(err)
	}
	c, err := localsif.New("relative" + string(os.PathListSeparator) + bin)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func ctx(t *testing.T) context.Context {
	c, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	t.Cleanup(cancel)
	return c
}

// A build keeps the SIF of the archive in the scratch directory, built in a directory of its own
// with apptainer's temporary files there, and leaves nothing else behind; a directory a killed
// build left a day ago is removed, and so is a SIF kept a day ago, and ones of now are not. A kept
// SIF is opened until it is removed.
func TestABuildKeepsTheSIFAndLeavesNothingElse(t *testing.T) {
	c := onPath(t)
	dir := filepath.Join(t.TempDir(), "scratch")
	stale, fresh := filepath.Join(dir, "vibesys-sif.old"), filepath.Join(dir, "vibesys-sif.new")
	staleSIF, freshSIF := filepath.Join(dir, "sif", "old.sif"), filepath.Join(dir, "sif", "new.sif")
	for _, d := range []string{stale, fresh, filepath.Join(dir, "sif")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for _, f := range []string{staleSIF, freshSIF} {
		if err := os.WriteFile(f, []byte("a SIF"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	old := time.Unix(1_000_000_000, 0)
	for _, p := range []string{stale, staleSIF} {
		if err := os.Chtimes(p, old, old); err != nil {
			t.Fatal(err)
		}
	}
	if _, ok, err := c.Open(ctx(t), dir, "image.sif"); ok || err != nil {
		t.Fatalf("a SIF never built opened: %v, %v", ok, err)
	}
	if err := c.Build(ctx(t), dir, strings.NewReader("a docker archive\n"), "image.sif"); err != nil {
		t.Fatal(err)
	}
	r, ok, err := c.Open(ctx(t), dir, "image.sif")
	if err != nil || !ok {
		t.Fatalf("the kept SIF: %v, %v", ok, err)
	}
	sif, _ := io.ReadAll(r)
	_ = r.Close()
	if string(sif) != "SIF of a docker archive\n" {
		t.Errorf("sif %q", sif)
	}
	entries, _ := os.ReadDir(dir)
	if names := entryNames(entries); !slices.Equal(names, []string{"sif", "vibesys-sif.new"}) {
		t.Errorf("left in the scratch directory: %v", names)
	}
	kept, _ := os.ReadDir(filepath.Join(dir, "sif"))
	if names := entryNames(kept); !slices.Equal(names, []string{"image.sif", "new.sif"}) {
		t.Errorf("kept: %v", names)
	}
	for range 2 {
		if err := c.Remove(ctx(t), dir, "image.sif"); err != nil {
			t.Errorf("remove: %v", err)
		}
	}
	if _, ok, err := c.Open(ctx(t), dir, "image.sif"); ok || err != nil {
		t.Errorf("a removed SIF opened: %v, %v", ok, err)
	}
	free, err := c.Free(ctx(t), filepath.Join(dir, "made"))
	if err != nil || free <= 0 {
		t.Errorf("free %d, %v", free, err)
	}
}

func entryNames(entries []os.DirEntry) []string {
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	return names
}

// A build of what is not an archive fails saying what apptainer said, keeps no SIF, and leaves
// nothing behind.
func TestAFailedBuildSaysWhyAndLeavesNothing(t *testing.T) {
	c := onPath(t)
	dir := t.TempDir()
	err := c.Build(ctx(t), dir, strings.NewReader("not an archive\n"), "image.sif")
	if err == nil || !strings.Contains(err.Error(), "FATAL: not a docker archive") {
		t.Errorf("build: %v", err)
	}
	if _, ok, err := c.Open(ctx(t), dir, "image.sif"); ok || err != nil {
		t.Errorf("a failed build kept a SIF: %v, %v", ok, err)
	}
	kept, _ := os.ReadDir(filepath.Join(dir, "sif"))
	if entries, _ := os.ReadDir(dir); len(entries) != 1 || len(kept) != 0 {
		t.Errorf("left in the scratch directory: %v, and kept %v", entryNames(entries), entryNames(kept))
	}
}

// The converter says its version and where it is.
func TestTheConverterSaysItsVersion(t *testing.T) {
	c := onPath(t)
	got, err := c.Describe(ctx(t))
	if err != nil || !strings.Contains(got, "1.4.5-fake") || !strings.Contains(got, "/apptainer)") {
		t.Errorf("describe %q, %v", got, err)
	}
}

// A machine without apptainer on PATH has no converter, and is told to install it.
func TestNoApptainerOnPathSaysToInstallIt(t *testing.T) {
	// A directory with no apptainer, and one with a file of the name that is not a program.
	empty, notProgram := t.TempDir(), t.TempDir()
	if err := os.WriteFile(filepath.Join(notProgram, "apptainer"), []byte("not a program"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := localsif.New(empty + string(os.PathListSeparator) + notProgram)
	if err == nil || !strings.Contains(err.Error(), "install Apptainer") {
		t.Errorf("new: %v", err)
	}
}

// The lock of a name is held by one open of it at a time, as by one process: another open is
// refused at once with ErrConverting until the holder lets go, and a lock of another name is
// another lock. A name that is not a file name is refused.
func TestALockIsHeldByOneAtATime(t *testing.T) {
	c := onPath(t)
	dir := filepath.Join(t.TempDir(), "scratch")
	held, err := c.Lock(ctx(t), dir, "image-a.lock")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Lock(ctx(t), dir, "image-a.lock"); !errors.Is(err, apptainer.ErrConverting) {
		t.Errorf("a second lock of a held name: %v, want ErrConverting", err)
	}
	other, err := c.Lock(ctx(t), dir, "image-b.lock")
	if err != nil {
		t.Errorf("a lock of another name: %v", err)
	} else {
		_ = other.Close()
	}
	if err := held.Close(); err != nil {
		t.Fatal(err)
	}
	again, err := c.Lock(ctx(t), dir, "image-a.lock")
	if err != nil {
		t.Fatalf("the lock after its holder let go: %v", err)
	}
	_ = again.Close()
	for _, name := range []string{"", ".", "..", "a/b"} {
		if _, err := c.Lock(ctx(t), dir, name); err == nil || errors.Is(err, apptainer.ErrConverting) {
			t.Errorf("the lock name %q: %v, want it refused", name, err)
		}
	}
}
