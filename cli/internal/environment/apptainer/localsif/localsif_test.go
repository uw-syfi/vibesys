//go:build linux || darwin

package localsif_test

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

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

// A build writes the SIF of the archive, built in a directory of its own under the scratch
// directory with apptainer's temporary files there, and leaves nothing behind; a directory a killed
// build left a day ago is removed, and one of now is not.
func TestABuildWritesTheSIFAndLeavesNothing(t *testing.T) {
	c := onPath(t)
	dir := filepath.Join(t.TempDir(), "scratch")
	stale, fresh := filepath.Join(dir, "vibesys-sif.old"), filepath.Join(dir, "vibesys-sif.new")
	for _, d := range []string{stale, fresh} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	old := time.Unix(1_000_000_000, 0)
	if err := os.Chtimes(stale, old, old); err != nil {
		t.Fatal(err)
	}
	var sif bytes.Buffer
	if err := c.Build(ctx(t), dir, strings.NewReader("a docker archive\n"), &sif); err != nil {
		t.Fatal(err)
	}
	if sif.String() != "SIF of a docker archive\n" {
		t.Errorf("sif %q", sif.String())
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 || entries[0].Name() != "vibesys-sif.new" {
		t.Errorf("left in the scratch directory: %v", entries)
	}
	free, err := c.Free(ctx(t), filepath.Join(dir, "made"))
	if err != nil || free <= 0 {
		t.Errorf("free %d, %v", free, err)
	}
}

// A build of what is not an archive fails saying what apptainer said, writes no SIF, and leaves
// nothing behind.
func TestAFailedBuildSaysWhyAndLeavesNothing(t *testing.T) {
	c := onPath(t)
	dir := t.TempDir()
	var sif bytes.Buffer
	err := c.Build(ctx(t), dir, strings.NewReader("not an archive\n"), &sif)
	if err == nil || !strings.Contains(err.Error(), "FATAL: not a docker archive") {
		t.Errorf("build: %v", err)
	}
	if sif.Len() != 0 {
		t.Errorf("a failed build wrote %q", sif.String())
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 0 {
		t.Errorf("left in the scratch directory: %v", entries)
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
