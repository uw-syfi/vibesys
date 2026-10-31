package server_test

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription/subscriptiontest"
	"google.golang.org/protobuf/proto"
)

var runsRequest = &apiv1.SubscribeRequest{Topic: &apiv1.SubscribeRequest_Runs{Runs: &apiv1.RunsTopic{}}}

// publishedRoot is a host root with one run whose head is published.
func publishedRoot(t *testing.T) string {
	t.Helper()
	root, run := t.TempDir(), t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "runs"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(run, filepath.Join(root, "runs", "run-1")); err != nil {
		t.Fatal(err)
	}
	data, err := proto.Marshal(head(proto.Uint64(1)))
	if err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(run, "run.head"), false, data)
	return root
}

// runsContract holds the runs topic to the contract every topic passes. It is event
// driven, so the cases for a sampled topic do not apply, and the change feed is
// scripted: the platform's watcher cannot run inside the contract's fake time.
func runsContract(t *testing.T) {
	subscriptiontest.Contract(t, func() subscriptiontest.Subject[*apiv1.RunEntry] {
		config := server.Config{Root: publishedRoot(t), Clock: clock.System{}}
		unwatchable := server.NewScriptedFeed()
		unwatchable.AddFails = errors.New("the root cannot be watched")
		return subscriptiontest.Subject[*apiv1.RunEntry]{
			Topic:   server.RunsTopicOver(config, server.NewScriptedFeed()),
			Request: runsRequest,
			Failing: server.RunsTopicOver(config, unwatchable).Source,
			Failure: apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED,
		}
	})
}
