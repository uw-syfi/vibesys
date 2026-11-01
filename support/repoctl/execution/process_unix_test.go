//go:build unix

package execution

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"
)

// A command whose child outlives it keeps the captured output open. With its own process
// group, cancelling the context must end the whole group and return promptly.
func TestCancellingAnOwnGroupCheckEndsItsDescendants(t *testing.T) {
	var out bytes.Buffer
	runner := OSRunner{Stdout: &out, Stderr: &out, OwnProcessGroup: true}
	ctx, cancel := context.WithCancel(context.Background())
	started := time.Now()
	go func() {
		for !strings.Contains(out.String(), "started") {
			time.Sleep(10 * time.Millisecond)
		}
		cancel()
	}()
	err := runner.Run(ctx, t.TempDir(), Check{Label: "x", Directory: ".", Args: []string{"sh", "-c", "echo started; sleep 300 & wait"}})
	if err == nil {
		t.Fatal("a cancelled check reported success")
	}
	if elapsed := time.Since(started); elapsed > time.Minute {
		t.Fatalf("returned after %s: a descendant kept the output open", elapsed)
	}
}
