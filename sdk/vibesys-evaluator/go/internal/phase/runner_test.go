package phase

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestAFaultGoesBetweenPhases(t *testing.T) {
	r := &Runner{Log: func(string) {}, Now: time.Now, Dir: t.TempDir()}
	ran := false
	action := func(context.Context) error { ran = true; return nil }
	if err := r.Fault(context.Background(), "restart", "kv", action); err == nil || ran {
		t.Fatalf("a fault before the first phase ran: %v", err)
	}
	r.next = 1
	if err := r.Fault(context.Background(), "Restart!", "kv", action); err == nil || ran {
		t.Fatalf("a fault of a bad kind ran: %v", err)
	}
	failing := func(context.Context) error { return errors.New("no such pod") }
	if err := r.Fault(context.Background(), "restart", "kv", failing); err == nil || !strings.Contains(err.Error(), "no such pod") {
		t.Fatalf("a failed action: %v", err)
	}
	if len(r.faults) != 1 || r.faults[0].GetAfterPhase() != 0 {
		t.Fatalf("the failed action was not recorded: %v", r.faults)
	}
}
