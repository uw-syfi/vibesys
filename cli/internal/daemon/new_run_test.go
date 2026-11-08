package daemon_test

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/uw-syfi/vibesys/cli/daemontest"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// DescribeTask answers Unimplemented until the task reader builds cards: never an empty success.
func TestDescribeTaskIsUnimplementedAndNamesItself(t *testing.T) {
	srv := daemontest.Server(t)
	ctx := context.Background()
	_, describe := srv.DescribeTask(ctx, &cliv1.DescribeTaskRequest{})

	for name, err := range map[string]error{"DescribeTask": describe} {
		if status.Code(err) != codes.Unimplemented || !strings.Contains(err.Error(), name) {
			t.Errorf("%s: got %v, want Unimplemented naming the call", name, err)
		}
	}
}
