package core

import "time"

// A task of one image may declare a job-scoped server ([service] in its manifest): a job starts it
// once and runs several of its commands against it, instead of each command starting a server of
// its own. The types below are that server as an environment starts it, and what a measurement
// records of the server each sample ran on.

// JobService is the job-scoped server as an environment starts it: a process of the task's image,
// over the job's checkout, that the commands run against reach over HTTP.
type JobService struct {
	Checkout Checkout
	// Artifacts, when set, is an empty directory outside the checkout that the server may leave
	// files in for a person to read after the job, as Execution.Artifacts is for a command.
	Artifacts string
	// Argv is the server's command, with the checkout's root resolved and PortPlaceholder not: the
	// environment chooses the port.
	Argv []string
	// Ready is the HTTP path that answers 2xx once the server is ready.
	Ready string
	// StartupTimeout bounds the wait for the server to be ready.
	StartupTimeout time.Duration
	// Lifetime is the longest the job may still use the server: an environment that places it in
	// an allocation with a time limit of its own bounds the allocation by it. It must be positive.
	Lifetime time.Duration
}

// SampleService is the server a sample of a task with a job-scoped server ran on. The set is
// closed; its zero value says the sample ran on none, or that its result does not say.
type SampleService string

// The servers a sample ran on.
const (
	// SampleServiceNone is a sample that ran on no job-scoped server, or whose result does not say.
	SampleServiceNone SampleService = ""
	// SampleServiceStarted is the first command on the server the job started.
	SampleServiceStarted SampleService = "started"
	// SampleServiceReused is a sample on a server an earlier command of the job used, after the
	// task's reset.
	SampleServiceReused SampleService = "reused"
	// SampleServiceRestarted is the first command on a server the job started again, because the one
	// an earlier command used had ended.
	SampleServiceRestarted SampleService = "restarted"
)

// SampleServices returns the servers of the samples of a and of b, in that order, as one series
// for a + b samples: nil when neither says, and otherwise one per sample, with SampleServiceNone
// for the samples of the one that does not say.
func SampleServices(a []SampleService, aSamples int, b []SampleService, bSamples int) []SampleService {
	if len(a) == 0 && len(b) == 0 {
		return nil
	}
	pad := func(s []SampleService, n int) []SampleService {
		if len(s) == n {
			return s
		}
		return make([]SampleService, n)
	}
	return append(append(make([]SampleService, 0, aSamples+bSamples), pad(a, aSamples)...), pad(b, bSamples)...)
}
