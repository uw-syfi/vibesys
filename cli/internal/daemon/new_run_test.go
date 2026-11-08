package daemon_test

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// DescribeTask and DescribeSetup answer Unimplemented until their readers exist: never an empty success.
func TestDescribeTaskAndDescribeSetupAreUnimplementedAndNameThemselves(t *testing.T) {
	srv := newRig(t).client
	ctx := context.Background()
	_, describe := srv.DescribeTask(ctx, &cliv1.DescribeTaskRequest{})
	_, setup := srv.DescribeSetup(ctx, &cliv1.DescribeSetupRequest{})
	for name, err := range map[string]error{"DescribeTask": describe, "DescribeSetup": setup} {
		if status.Code(err) != codes.Unimplemented || !strings.Contains(err.Error(), name) {
			t.Errorf("%s: got %v, want Unimplemented naming the call", name, err)
		}
	}
}
