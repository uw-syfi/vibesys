package clidaemontest_test

import (
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"os"
	"path/filepath"
	"testing"
)

func TestContract(t *testing.T) {
	clidaemontest.Contract(t, func(*testing.T) clidaemon.Service { return clidaemontest.New() })
}
func TestCorpus(t *testing.T) {
	files, err := filepath.Glob("../../../../proto/vibesys/cli/v1/conformance/task_watch/*.json")
	if err != nil || len(files) == 0 {
		t.Fatalf("no corpus: %v", err)
	}
	ran := 0
	for _, file := range files {
		t.Run(filepath.Base(file), func(t *testing.T) {
			raw, err := os.ReadFile(file)
			if err != nil {
				t.Fatal(err)
			}
			clidaemontest.Corpus(t, raw)
			ran++
		})
	}
	if ran != len(files) {
		t.Fatalf("ran %d of %d corpus files", ran, len(files))
	}
}
