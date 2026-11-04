package main

import (
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

func TestAWaitSaysWhatTheJobWaitsForAndWho(t *testing.T) {
	measure := &cliv1.WaitHolder{JobId: "j41", Task: "t", Kind: cliv1.JobKind_JOB_KIND_MEASURE, RunId: "B"}
	check := &cliv1.WaitHolder{JobId: "j3", Task: "other", Kind: cliv1.JobKind_JOB_KIND_CHECK}
	for want, w := range map[string]*cliv1.WaitReason{
		"placement:default held exclusively by j41 (measure, run B)": {
			Kind: cliv1.WaitKind_WAIT_KIND_HELD_EXCLUSIVELY, Resource: "placement:default", Holders: []*cliv1.WaitHolder{measure},
		},
		"needs placement:default alone, in use by j3 of task other (check), j41 (measure, run B)": {
			Kind: cliv1.WaitKind_WAIT_KIND_IN_USE, Resource: "placement:default", Holders: []*cliv1.WaitHolder{check, measure},
		},
		"placement:default is full, held by j41 (measure, run B), j41 (measure, run B), j41 (measure, run B), 1 more": {
			Kind: cliv1.WaitKind_WAIT_KIND_FULL, Resource: "placement:default", Holders: []*cliv1.WaitHolder{measure, measure, measure, measure},
		},
		"behind j41 (measure, run B), which waits for placement:default": {
			Kind: cliv1.WaitKind_WAIT_KIND_BEHIND, Resource: "placement:default", Holders: []*cliv1.WaitHolder{measure},
		},
		"it stays queued until its store can be read: away": {Kind: cliv1.WaitKind_WAIT_KIND_TROUBLE, Detail: "it stays queued until its store can be read: away"},
		"placement:x, j41 (measure, run B), new":            {Kind: cliv1.WaitKind(99), Resource: "placement:x", Holders: []*cliv1.WaitHolder{measure}, Detail: "new"},
		"needs placement:default alone, leased by bob on host-b (pid 4242), job j7": {
			Kind: cliv1.WaitKind_WAIT_KIND_LEASED, Resource: "placement:default", Elsewhere: &cliv1.LeaseHolder{User: "bob", Host: "host-b", Pid: 4242, JobId: "j7"},
		},
		"needs placement:default alone: the lease could not be taken": {
			Kind: cliv1.WaitKind_WAIT_KIND_LEASED, Resource: "placement:default", Detail: "the lease could not be taken",
		},
		"a filter": {Kind: cliv1.WaitKind_WAIT_KIND_HELD, Detail: "a filter"},
	} {
		if got := describeWait(w, "t"); got != want {
			t.Errorf("%v: %q, want %q", w, got, want)
		}
	}
}
