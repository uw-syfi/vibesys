// Package apptainer places the docker environment's containers on a Slurm cluster whose compute
// nodes run Apptainer and no Docker, reached through a login node over ssh, with a file system the
// login node and the compute nodes share. Engine is a docker.Engine, so the docker environment's
// decisions (the images, the two containers of a task of two images, the resources each gets, the
// stop on every path) are the same here as on a machine with Docker.
//
// What a container is here:
//
//   - The images are built by the caller's own Docker, as everywhere, and each is converted to a
//     SIF on the caller's machine with its apptainer program, in a local directory checked first
//     for room for the image unpacked, where the SIF is kept until it is copied to the cluster,
//     once the conversion has ended, named by the digest of the image's content (images/<hex>.sif
//     under the remote root), not by its ID: a run that finds it there converts and copies nothing.
//     A copy that loses its connection is made again, a bounded number of times, and a SIF never
//     copied is kept for the next run. The publishers of one image on one machine convert it one at
//     a time, under a lock of the machine, and a publisher that waited uses the SIF of the one it
//     waited for. It is published whole by a rename, so publishers of different machines at once
//     each write a file of their own and one of them wins. The cluster's apptainer only runs SIFs.
//   - A run's first container is a batch allocation of its own (sbatch, or the site's command that
//     wraps it), named by the container and commented with the scope, never requeued. Once it runs,
//     a holding process is started on its node, whose input the caller keeps: when the caller closes
//     it, or dies, the holder cancels the allocation. The second container of a task of two images
//     joins the first one's allocation, so both run on one node and share its network.
//   - The node reads its own facts and load, which is what the environment records of the machine.
//   - A container's processes are steps of the allocation (srun --overlap) that run apptainer exec
//     of the image, read-only, with a clean environment, the container's mounts bound, a private
//     /tmp that is its home, and a variable naming the container in every process, by which every
//     one of them is sent SIGTERM to stop and SIGKILL to be removed.
//   - Removing a run's first container cancels its allocation by name and confirms that it ended
//     (squeue, then sacct); a run first removes what a dead run of its scope left, which is every
//     allocation of the scope that no live run owns. Where a placement runs several jobs at once,
//     each job's runs are a scope of their own under the placement's (Config.Runs), and a run
//     removes the allocations of its own scope and of the placement's dead runs, never a live
//     one's.
//   - A standing allocation (standing.go) is a batch allocation of a run's, not of a container's: it
//     runs no holder, so no caller's exit cancels it, and a run's sweep never removes it. A job
//     whose container names one (LabelAllocation) runs in it as a second container joins the
//     first one's allocation; removing the container kills its processes, confirmed, and leaves
//     the allocation running.
//   - An exit code that is the transport's own (ssh's 255), and a step that never started its
//     command, are not the command's ending: the engine reports them as lost.
package apptainer

import (
	"context"
	"errors"
	"io"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// Cluster is the cluster as the engine uses it: files on the file system its nodes share, batch
// allocations, and the node of a running one. Every call answers by a bound of its own, except
// Publish, which copies as long as the data needs, and reports a cluster it cannot reach as a
// *core.Failure of kind FailureUnavailable. The real one runs Slurm's and Apptainer's programs
// through ssh (clustercli); a Fake keeps a cluster in memory.
type Cluster interface {
	// Has reports whether a file is at the path, an absolute path of the shared file system.
	Has(ctx context.Context, path string) (bool, error)
	// Read returns the content of a small file of the shared file system, and false when there is
	// none.
	Read(ctx context.Context, path string) ([]byte, bool, error)
	// Publish makes the path a file of what r gives, whole: a copy that r or the transport cuts
	// short leaves the path as it was. Publishers of one path at once each write a file of their
	// own, and the path is one of them, whole. A copy whose connection to the cluster failed
	// returns an error that wraps ErrConnectionLost.
	Publish(ctx context.Context, path string, r io.Reader) error
	// Squash makes the path an Enroot squashfs of the root file system rootfs gives (a tar archive
	// that RootFS wrote, whose last entry is ImageMarker holding content), whole: one made from a
	// stream cut short, or whose marker does not hold content, is never there. A path that is
	// already there is kept, and rootfs is then not read to its end. Squashes of one path at once
	// on one login node make it one at a time. It returns what made the squashfs, with its
	// version, for a person.
	Squash(ctx context.Context, path, content string, rootfs io.Reader) (string, error)

	// Submit asks for a batch allocation that holds one node until it is cancelled or its time
	// limit, and returns the job's id. Slurm never requeues it.
	Submit(ctx context.Context, a Allocation) (string, error)
	// State is the job's state now.
	State(ctx context.Context, job string) (JobState, error)
	// Jobs returns the user's pending and running jobs of the scope: those whose comment is the
	// scope, or a run's scope under it (InScope).
	Jobs(ctx context.Context, scope string) ([]Job, error)
	// Cancel cancels the user's jobs of the name, and returns once Slurm confirms that none of them
	// is pending or running. A name no job has is cancelled.
	Cancel(ctx context.Context, name string) error

	// Node is the node of the running job.
	Node(job string) Node

	// Detail is what Slurm says of the job now, with its times as instants: the state of a
	// standing allocation. A job Slurm knows nothing of has ended.
	Detail(ctx context.Context, job string) (JobDetail, error)
	// CancelJob cancels the user's job of the id, and returns once Slurm confirms that it is
	// neither pending nor running (squeue, then sacct). A job that is not there is cancelled.
	CancelJob(ctx context.Context, job string) error
	// Partition is what Slurm says of the partition's capacity now: its nodes, the jobs waiting
	// for it and the reservations. The empty partition is the cluster's default.
	Partition(ctx context.Context, partition string) (PartitionFacts, error)
}

// Allocation is a batch allocation to ask for.
type Allocation struct {
	// Name is the job's name, unique to the container whose allocation it is.
	Name string
	// Comment is the scope, or a run's scope under it (RunComment), which Jobs looks for.
	Comment string
	// Limit is the job's time limit.
	Limit time.Duration
	// Options are the submit command's options that size the allocation: the partition, the GRES,
	// the CPUs and the memory, or the whole node.
	Options []string
	// Idle, when set, makes the allocation reusable: it ends itself once no container has claimed
	// it (Node.Claim) for this long, and is never held (Node.Hold).
	Idle time.Duration
}

// Job is a job of the user's that Jobs lists.
type Job struct {
	ID, Name string
	// Comment is the job's comment: its scope, or a run's scope under it.
	Comment string
	Phase   Phase
	// Left is the time left before the job's time limit, as Slurm says it while the job runs.
	Left time.Duration
}

// Phase is where a job is in its life.
type Phase int

const (
	// Queued is a job that waits for its allocation.
	Queued Phase = iota
	// Running is a job that holds its node.
	Running
	// Ended is a job that runs no more, or that Slurm no longer knows.
	Ended
)

// JobState is a job's state.
type JobState struct {
	Phase Phase
	// Name is Slurm's name of the state, such as PENDING or CANCELLED, when it gave one.
	Name string
	// Reason is why a queued job waits, as Slurm says it.
	Reason string
	// Start is when Slurm estimates a queued job starts, as it writes the time (in the cluster's
	// time zone), when it gives an estimate.
	Start string
	// Node is the node of a running job, as Slurm names it.
	Node string
}

// Node is the node of a running allocation. Each call runs on the node, inside the allocation.
type Node interface {
	// Hold starts the process that holds the allocation for the caller, and returns once it runs
	// on the node. Closing what it returns, or the caller's death, makes the process end every
	// container of the allocation and cancel the allocation.
	Hold(ctx context.Context) (io.Closer, error)
	// Claim claims a reusable allocation (Allocation.Idle) for one container, and returns once it
	// holds the claim, which every caller of every process and machine shares: a claim that another
	// holds, or of an allocation that is ending, fails with ErrTaken. Before it returns, every
	// process of a container the allocation ran (what a dead claim left) is killed and gone, and the
	// containers' homes are removed. Closing what it returns releases the claim the same way, killing
	// every process of a container and waiting for none to be left, and returns an error when the
	// release was not confirmed; the caller's death releases it too, or leaves a dead claim that
	// the allocation counts as released.
	Claim(ctx context.Context) (io.Closer, error)
	// Read runs each script with /bin/sh on the node and returns what each printed, by its name.
	Read(ctx context.Context, scripts map[string]string) (map[string]string, error)
	// Port returns a TCP port on which no process of the node listens now.
	Port(ctx context.Context) (int, error)
	// Start starts argv in a container of the box on the node, with its standard output and
	// standard error written to sink as one stream. A process whose step could not start the
	// command, or whose end the transport lost, reports an Exit whose Err is docker.ErrLost.
	Start(box Box, argv []string, sink io.Writer) (environment.Process, error)
	// Signal sends the signal to every process of the box's containers on the node. A box with no
	// process is signalled.
	Signal(ctx context.Context, box string, sig Signal) error
	// Reap kills every process of the box's containers on the node and waits for none to be left,
	// as long as a minute, and fails when some are left. A box with no process is reaped. The
	// allocation is left as it is.
	Reap(ctx context.Context, box string) error
}

// Signal is a signal Node.Signal sends.
type Signal string

// The signals a box's processes are sent.
const (
	SIGTERM Signal = "TERM"
	SIGKILL Signal = "KILL"
)

// Box is what a container of the node is made of: every process of it runs in the image with these.
type Box struct {
	// Name names the box; every one of its processes carries it, in EnvBox.
	Name string
	// Image is the path of the image on the shared file system, run read-only: a SIF under the
	// apptainer runtime, an Enroot squashfs under pyxis.
	Image string
	// Dir is the working directory of every process.
	Dir string
	// Mounts are the directories of the node bound in the container.
	Mounts []docker.Mount
	// Env is set for every process, over the image's own environment, which is otherwise clean.
	Env map[string]string
	// ROCm gives the box the node's AMD GPUs (apptainer --rocm; under pyxis, the site's Enroot
	// binds the node's /dev into every container), and the variables in which Slurm names the GPUs
	// it assigned, when it set them.
	ROCm bool
}

// ErrTaken says that a reusable allocation is claimed by another container, or is ending.
var ErrTaken = errors.New("apptainer: the allocation is claimed by another container, or is ending")

// EnvBox names the variable every process of a box carries, whose value is the box's name.
const EnvBox = "VIBESYS_BOX"

// AssignedGPUVariables are the variables in which Slurm names the GPUs it assigned a job, when it
// does: a box that gets the GPUs gets each that is set where its process starts. None is required,
// since a partition that does not isolate its nodes' GPUs sets none.
var AssignedGPUVariables = []string{"ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL", "CUDA_VISIBLE_DEVICES"}

// RunComment is the comment of an allocation of the run of the scope run, of a placement whose
// scope is scope: the placement's scope and the run's, so that Jobs of the placement's scope finds
// the allocations of every one of its runs, and each says whose it is.
func RunComment(scope, run string) string {
	if run == "" || run == scope {
		return scope
	}
	return scope + runSeparator + run
}

// runSeparator separates a placement's scope from a run's in a comment. No scope holds it.
const runSeparator = "."

// InScope reports whether a job's comment is of the scope, and names the run of the scope it
// belongs to: the scope itself for a comment that is the scope alone.
func InScope(comment, scope string) (run string, ok bool) {
	if comment == scope {
		return scope, true
	}
	run, ok = strings.CutPrefix(comment, scope+runSeparator)
	return run, ok && run != ""
}
