package daemontest_test

import (
	"context"
	"errors"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

func TestAHeldReadSeesTheFactsAsTheyWereWhenItStarted(t *testing.T) {
	f := daemontest.NewFakeSetupReads(setup.Facts{Version: "v1"})
	var got []setup.Read
	collect := func(r setup.Read) { got = append(got, r) }
	ref := daemontest.SetupReadRef{Repository: "/repo", Task: "t"}

	f.Block()
	f.Start(context.Background(), ref, collect)
	f.EditTask("v2")
	f.Start(context.Background(), ref, collect)
	if f.Held() != 2 || len(got) != 0 {
		t.Fatalf("held %d, delivered %d", f.Held(), len(got))
	}
	f.ReleaseAt(1)
	f.Release()
	if len(got) != 2 || got[0].Facts.Version != "v2" || got[1].Facts.Version != "v1" {
		t.Fatalf("%+v", got)
	}
	f.Start(context.Background(), ref, collect)
	if len(got) != 3 || got[2].Facts.Version != "v2" || len(f.Calls()) != 3 {
		t.Fatalf("after release a read must complete at once: %+v", got)
	}
}

func TestFailureAndUnreadableAreAnsweredAsReads(t *testing.T) {
	f := daemontest.NewFakeSetupReads(setup.Facts{})
	var got setup.Read
	f.Fail(errors.New("away"))
	f.Start(context.Background(), daemontest.SetupReadRef{Task: "t"}, func(r setup.Read) { got = r })
	if got.Kind != setup.ReadFailure || got.Detail != "away" {
		t.Fatalf("%+v", got)
	}
	f.Fail(nil)
	f.Unreadable("half written")
	f.Start(context.Background(), daemontest.SetupReadRef{Task: "t"}, func(r setup.Read) { got = r })
	if got.Kind != setup.ReadUnreadable || got.Detail != "half written" {
		t.Fatalf("%+v", got)
	}
}
