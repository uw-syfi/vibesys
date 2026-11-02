//go:build unix

package envtest

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// helperEnv carries the execution a helper process runs. A process that has it set is a helper.
const helperEnv = "ENVTEST_EXECUTION"

// HelperMain makes the test binary able to be the process that Spawn kills. Call it first in
// TestMain: when it returns true this process was started by SpawnProcess, has run the execution
// in the environment newEnv makes, and the caller exits at once without running tests.
//
//	func TestMain(m *testing.M) {
//		if envtest.HelperMain(func() environment.Environment { return mine.New(...) }) {
//			return
//		}
//		os.Exit(m.Run())
//	}
func HelperMain(newEnv func() environment.Environment) bool {
	raw, ok := os.LookupEnv(helperEnv)
	if !ok {
		return false
	}
	var x core.Execution
	if err := json.Unmarshal([]byte(raw), &x); err != nil {
		os.Stderr.WriteString("envtest helper: " + err.Error() + "\n")
		os.Exit(2)
	}
	_, _ = newEnv().Run(context.Background(), x, io.Discard)
	return true
}

// SpawnProcess starts a copy of the test binary as a helper (see HelperMain) that runs the
// execution, and returns the function that kills it with SIGKILL and waits for it to be gone. The
// test also kills it when it ends. What the execution has started is not the helper's to end:
// that is what the suite checks.
func SpawnProcess(t testing.TB, x core.Execution) (kill func()) {
	t.Helper()
	raw, err := json.Marshal(x)
	if err != nil {
		t.Fatalf("encoding the execution: %v", err)
	}
	// -test.run matches nothing: if the binary has no HelperMain it runs no test, and the
	// absence of the process the suite waits for fails the case clearly.
	cmd := exec.Command(os.Args[0], "-test.run=^$")
	cmd.Env = append(os.Environ(), helperEnv+"="+string(raw))
	if err := cmd.Start(); err != nil {
		t.Fatalf("starting the helper process: %v", err)
	}
	var once sync.Once
	kill = func() {
		once.Do(func() {
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
		})
	}
	t.Cleanup(kill)
	return kill
}
