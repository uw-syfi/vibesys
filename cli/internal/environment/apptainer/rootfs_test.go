package apptainer_test

import (
	"archive/tar"
	"bytes"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/propcheck"
)

// entry is a file of a tar archive, as the tests compare them.
type entry struct{ name, data string }

func entriesOf(t *testing.T, archive []byte) []entry {
	t.Helper()
	var out []entry
	r := tar.NewReader(bytes.NewReader(archive))
	for {
		h, err := r.Next()
		if errors.Is(err, io.EOF) {
			return out
		}
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(r)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, entry{h.Name, string(body)})
	}
}

// names an export may hold, each spelled as docker export or another tar writer may spell it: what
// Enroot's import leaves out or writes itself among them.
var exportNames = []string{
	"bin/", "bin/sh", "./usr/lib/x.so", "/opt/app", "etc/", "./etc/", "etc/passwd", "etc/environment", "./etc/environment",
	"etc/fstab", ".dockerenv", "./.dockerenv", "dev/", "dev/null", "./dev/shm/", "devices/x", ".vibesys-image", "tmp/",
}

// dropped reports whether the root file system leaves out an export's entry of the name.
func dropped(name string) bool {
	n := strings.TrimSuffix(strings.TrimPrefix(strings.TrimPrefix(name, "/"), "./"), "/")
	return n == ".dockerenv" || n == "etc/environment" || n == "etc/fstab" || n == apptainer.ImageMarker || strings.HasPrefix(n, "dev/")
}

// The root file system of an image is its export, in order, but for what Enroot leaves out or
// writes itself, followed by the image's environment, an empty fstab, and last the marker of its
// content, whatever the export holds and however it spells its names.
func TestTheRootFileSystemIsTheExportAsEnrootRunsIt(t *testing.T) {
	err := quick.Check(func(seed uint64, n uint8, envN uint8) bool {
		rnd := rand.New(rand.NewPCG(seed, 1))
		var export bytes.Buffer
		tw := tar.NewWriter(&export)
		var want []entry
		hasEtc := false
		for i := range int(n % 24) {
			name := exportNames[rnd.IntN(len(exportNames))]
			data := ""
			typ := byte(tar.TypeDir)
			if !strings.HasSuffix(name, "/") {
				data, typ = fmt.Sprintf("file %d of %s", i, name), tar.TypeReg
			}
			if err := tw.WriteHeader(&tar.Header{Name: name, Typeflag: typ, Mode: 0o644, Size: int64(len(data))}); err != nil {
				t.Fatal(err)
			}
			_, _ = io.WriteString(tw, data)
			if !dropped(name) {
				want = append(want, entry{name, data})
			}
			hasEtc = hasEtc || strings.TrimSuffix(strings.TrimPrefix(name, "./"), "/") == "etc"
		}
		_ = tw.Close()
		var env []string
		for i := range int(envN % 5) {
			env = append(env, fmt.Sprintf("K%d=v %d=x", i, rnd.IntN(100)))
		}
		const content = "sha256:0123"
		var out bytes.Buffer
		if err := apptainer.RootFS(&export, env, content, &out); err != nil {
			t.Fatal(err)
		}
		if !hasEtc {
			want = append(want, entry{"etc/", ""})
		}
		environ := ""
		for _, kv := range env {
			environ += kv + "\n"
		}
		want = append(want, entry{"etc/environment", environ}, entry{"etc/fstab", ""}, entry{apptainer.ImageMarker, content + "\n"})
		got := entriesOf(t, out.Bytes())
		if !slices.Equal(got, want) {
			t.Logf("entries\n%q\nwant\n%q", got, want)
			return false
		}
		return true
	}, propcheck.Config(t, nil))
	if err != nil {
		t.Error(err)
	}
}

// An environment that is not KEY=VALUE lines is refused before anything is written, and an export
// cut short fails the root file system rather than ending it early.
func TestARootFileSystemIsNeverMadeOfWhatItCannotHold(t *testing.T) {
	for _, env := range [][]string{{"NOEQUALS"}, {"=v"}, {"K=a\nb"}} {
		var out bytes.Buffer
		if err := apptainer.RootFS(strings.NewReader(""), env, "sha256:0", &out); err == nil || out.Len() != 0 {
			t.Errorf("environment %q: %v, wrote %d bytes", env, err, out.Len())
		}
	}
	var export bytes.Buffer
	tw := tar.NewWriter(&export)
	_ = tw.WriteHeader(&tar.Header{Name: "big", Typeflag: tar.TypeReg, Mode: 0o644, Size: 4096})
	_, _ = tw.Write(make([]byte, 4096))
	_ = tw.Close()
	cut := export.Bytes()[:1024]
	var out bytes.Buffer
	if err := apptainer.RootFS(bytes.NewReader(cut), nil, "sha256:0", &out); err == nil {
		t.Error("an export cut short made a root file system")
	}
	if bytes.Contains(out.Bytes(), []byte(apptainer.ImageMarker)) {
		t.Error("a root file system of an export cut short has the marker of a whole one")
	}
}
