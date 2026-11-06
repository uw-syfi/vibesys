package k8s

import (
	"flag"
	"os"
	"os/exec"
	"testing"
)

var update = flag.Bool("update", false, "rewrite api/go-k8s.txt")

const snapshot = "../../api/go-k8s.txt"

// The public Go API is api/go-k8s.txt: what `go doc -all` says of this package. A change to it is
// reviewed there; a breaking one needs the user's approval first (sdk/vibesys-evaluator/AGENTS.md).
// `go test ./k8s -run APISnapshot -update` rewrites it.
func TestAPISnapshotIsCurrent(t *testing.T) {
	goTool, err := exec.LookPath("go")
	if err != nil {
		t.Skip("go is not on PATH")
	}
	cmd := exec.Command(goTool, "doc", "-all", ".")
	cmd.Env = append(os.Environ(), "GOFLAGS=-mod=mod")
	listing, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	if *update {
		if err := os.WriteFile(snapshot, listing, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	current, err := os.ReadFile(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if string(current) != string(listing) {
		t.Fatalf("api/go-k8s.txt differs from `go doc -all` of the package: review the change, then rerun with -update\n%s", listing)
	}
}
