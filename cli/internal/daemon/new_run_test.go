package daemon_test

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// The calls of the New run contracts answer Unimplemented, naming themselves, until the reader and
// the probes exist: never an empty success a client would read as "no card" or "no places".
func TestTheNewRunCallsAreUnimplementedAndNameThemselves(t *testing.T) {
	srv := newRig(t).client
	ctx := context.Background()
	_, describe := srv.DescribeTask(ctx, &cliv1.DescribeTaskRequest{})
	_, list := srv.ListPlacements(ctx, &cliv1.ListPlacementsRequest{})
	for name, err := range map[string]error{"DescribeTask": describe, "ListPlacements": list} {
		if status.Code(err) != codes.Unimplemented || !strings.Contains(err.Error(), name) {
			t.Errorf("%s: got %v, want Unimplemented naming the call", name, err)
		}
	}
}
