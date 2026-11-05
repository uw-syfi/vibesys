// Package apptainer places the docker environment's containers on a Slurm cluster whose compute
// nodes run Apptainer and no Docker, reached through a login node over ssh, with a file system the
// login node and the compute nodes share. Engine is a docker.Engine, so the docker environment's
// decisions (the images, the two containers of a task of two images, the resources each gets, the
// stop on every path) are the same here as on a machine with Docker.
//
// What a container is here:
//
//   - The images are built by the caller's own Docker, as everywhere, and each is converted to a SIF
//     on the caller's machine with its apptainer program, in a local directory checked first for
//     room for the image unpacked, where the SIF is kept until it is copied to the cluster, once
//     the conversion has ended, named by its digest (images/<hex>.sif under the remote root): a
//     run that finds it there converts and copies nothing. A copy that loses its connection is
//     made again, a bounded number of times, and a SIF never copied is kept for the next run. The publishers of one image on one machine convert it one at a time, under a lock of
//     the machine, and a publisher that waited uses the SIF of the one it waited for. It is
//     published whole by a rename, so publishers of different machines at once each write a file of
//     their own and one of them wins. The cluster's apptainer only runs SIFs.
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
//     allocation of the scope that no live run owns.
//   - An exit code that is the transport's own (ssh's 255), and a step that never started its
//     command, are not the command's ending: the engine reports them as lost.
package apptainer

import (
	"context"
	"io"
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

	// Submit asks for a batch allocation that holds one node until it is cancelled or its time
	// limit, and returns the job's id. Slurm never requeues it.
	Submit(ctx context.Context, a Allocation) (string, error)
	// State is the job's state now.
	State(ctx context.Context, job string) (JobState, error)
	// Jobs returns the names of the user's pending and running jobs whose comment is the scope.
	Jobs(ctx context.Context, scope string) ([]string, error)
	// Cancel cancels the user's jobs of the name, and returns once Slurm confirms that none of them
	// is pending or running. A name no job has is cancelled.
	Cancel(ctx context.Context, name string) error

	// Node is the node of the running job.
	Node(job string) Node
}

// Allocation is a batch allocation to ask for.
type Allocation struct {
	// Name is the job's name, unique to the container whose allocation it is.
	Name string
	// Comment is the scope, which Jobs looks for.
	Comment string
	// Limit is the job's time limit.
	Limit time.Duration
	// Options are the submit command's options that size the allocation: the partition, the GRES,
	// the CPUs and the memory, or the whole node.
	Options []string
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
}

// Node is the node of a running allocation. Each call runs on the node, inside the allocation.
type Node interface {
	// Hold starts the process that holds the allocation for the caller, and returns once it runs
	// on the node. Closing what it returns, or the caller's death, makes the process end every
	// container of the allocation and cancel the allocation.
	Hold(ctx context.Context) (io.Closer, error)
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
	// Image is the path of the SIF on the shared file system, run read-only.
	Image string
	// Dir is the working directory of every process.
	Dir string
	// Mounts are the directories of the node bound in the container.
	Mounts []docker.Mount
	// Env is set for every process, over the image's own environment, which is otherwise clean.
	Env map[string]string
	// ROCm gives the box the node's AMD GPUs (apptainer --rocm), and the variables in which Slurm
	// names the GPUs it assigned, when it set them.
	ROCm bool
}

// EnvBox names the variable every process of a box carries, whose value is the box's name.
const EnvBox = "VIBESYS_BOX"

// AssignedGPUVariables are the variables in which Slurm names the GPUs it assigned a job, when it
// does: a box that gets the GPUs gets each that is set where its process starts. None is required,
// since a partition that does not isolate its nodes' GPUs sets none.
var AssignedGPUVariables = []string{"ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL", "CUDA_VISIBLE_DEVICES"}
