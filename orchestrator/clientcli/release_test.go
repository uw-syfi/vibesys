package clientcli_test

import (
	"bytes"
	"context"
	"io/fs"
	"runtime"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

// installedFS lays out, in memory at /rel, a complete installed release.
func installedFS(t *testing.T, goos, goarch string, without ...string) fstest.MapFS {
	t.Helper()
	return releasetest.Tree(t, "/rel", releasetest.Platform(goos, goarch), releasetest.Version("3.4.5"), releasetest.Without(without...))
}

func verify(fsys fs.FS, args ...string) (code int, stdout, stderr string) {
	var out, errb bytes.Buffer
	code = clientcli.Main(context.Background(), append([]string{"release", "verify"}, args...), clientcli.Env{
		Stdout: &out, Stderr: &errb, Dir: "/", FS: fsys,
	})
	return code, out.String(), errb.String()
}

func TestReleaseVerifyPrintsTheVersionOfAWholeReleaseAndNothingElse(t *testing.T) {
	for _, flags := range [][]string{{"/rel"}, {"--installed", "/rel"}, {"rel"}} {
		code, out, errb := verify(installedFS(t, runtime.GOOS, runtime.GOARCH), flags...)
		if code != clientcli.ExitOK || out != "3.4.5\n" || errb != "" {
			t.Fatalf("%v: exit %d, stdout %q, stderr %q", flags, code, out, errb)
		}
	}
}

// A release before install is whole without its session holder's environment; after install it
// is not. Either way the file is named and nothing is printed on stdout.
func TestReleaseVerifyNamesWhatIsMissing(t *testing.T) {
	holder := "sidecar/.venv/bin/python"
	fsys := installedFS(t, runtime.GOOS, runtime.GOARCH, holder, "bin/vibesys-host")
	code, out, errb := verify(fsys, "/rel")
	if code != clientcli.ExitFailed || out != "" || !strings.Contains(errb, "bin/vibesys-host") || strings.Contains(errb, holder) {
		t.Fatalf("archive: exit %d, stdout %q, stderr %q", code, out, errb)
	}
	code, out, errb = verify(fsys, "--installed", "/rel")
	if code != clientcli.ExitFailed || out != "" || !strings.Contains(errb, "bin/vibesys-host") || !strings.Contains(errb, holder) {
		t.Fatalf("installed: exit %d, stdout %q, stderr %q", code, out, errb)
	}
}

func TestReleaseVerifyRefusesAnotherPlatformAndAFolderWithNoRelease(t *testing.T) {
	other := "arm64"
	if runtime.GOARCH == "arm64" {
		other = "amd64"
	}
	code, _, errb := verify(installedFS(t, runtime.GOOS, other), "/rel")
	if code != clientcli.ExitFailed || !strings.Contains(errb, runtime.GOOS+"/"+other) {
		t.Fatalf("exit %d, stderr %q", code, errb)
	}
	code, _, errb = verify(fstest.MapFS{}, "/nowhere")
	if code != clientcli.ExitFailed || !strings.Contains(errb, "/nowhere/release.json") {
		t.Fatalf("exit %d, stderr %q", code, errb)
	}
	if code, _, _ := verify(installedFS(t, runtime.GOOS, runtime.GOARCH)); code != clientcli.ExitUsage {
		t.Fatalf("a missing folder is a usage error, got %d", code)
	}
}
