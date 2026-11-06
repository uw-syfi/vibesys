// Package k8s deploys a task's application into a Kubernetes cluster and acts on it: it builds
// the images with BuildKit, applies the manifest into a namespace of its own, waits until the
// application serves, runs the package's pods (seed, probe, phase) in that namespace, applies
// fault actions (restart one or every pod of a Deployment, exec a command in a pod, wait for
// readiness again) and deletes the namespace on every exit path.
//
// It is the Go core behind the Python `microservices.k8s.App` and the `vibesys-evaluator k8s`
// subcommands. kubectl and buildctl run as subprocesses, behind the KubeAPI and Builder
// interfaces; package k8stest holds an in-memory cluster and builder for tests.
//
// The cluster is the one the environment gives: KUBECONFIG, VIBESYS_K8S_REGISTRY and
// VIBESYS_K8S_BUILDER (see ParseEnvironment).
package k8s

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// Error is a failure of the deploy layer: the system under test could not be brought up, acted on
// or torn down. The message names the step and ends with the relevant log tail. Kind groups
// failures for the language wrappers (see the Kind constants).
type Error struct {
	Kind    string
	Message string
}

// Error kinds, as the `error_kind` of a subcommand's response.
const (
	KindDeploy      = "deploy"
	KindBuild       = "build"
	KindManifest    = "manifest"
	KindInvalid     = "invalid"
	KindOutOfMemory = "out_of_memory"
	KindKube        = "kube"
	KindCanceled    = "canceled"
)

func (e *Error) Error() string { return e.Message }

func deployErr(format string, args ...any) error {
	return &Error{Kind: KindDeploy, Message: fmt.Sprintf(format, args...)}
}

// OutOfMemoryError is a pod of the package that was OOMKilled; Limit is its memory limit ("4Gi").
type OutOfMemoryError struct {
	Message string
	Limit   string
}

func (e *OutOfMemoryError) Error() string { return e.Message }

// KindOf is the error kind of err, KindDeploy for an error of no known kind.
func KindOf(err error) string {
	var (
		e   *Error
		oom *OutOfMemoryError
		kub *KubeError
		bld *BuildError
		man *ManifestError
	)
	switch {
	case errors.Is(err, context.Canceled):
		return KindCanceled
	case errors.As(err, &e):
		return e.Kind
	case errors.As(err, &oom):
		return KindOutOfMemory
	case errors.As(err, &bld):
		return KindBuild
	case errors.As(err, &man):
		return KindManifest
	case errors.As(err, &kub):
		return KindKube
	}
	return KindDeploy
}

// KubeError is what the cluster refused or failed; the message says which and why.
type KubeError struct{ Message string }

func (e *KubeError) Error() string { return e.Message }

// ErrNotFound and ErrConflict classify a KubeError with errors.Is: the object does not exist, or
// a precondition failed (it is not the object the caller meant: another UID).
var (
	ErrNotFound = errors.New("not found")
	ErrConflict = errors.New("conflict")
)

type classified struct {
	*KubeError
	class error
}

func (c classified) Is(target error) bool { return target == c.class }
func (c classified) Unwrap() error        { return c.KubeError }

// NotFound returns a KubeError that matches ErrNotFound.
func NotFound(format string, args ...any) error {
	return classified{&KubeError{fmt.Sprintf(format, args...)}, ErrNotFound}
}

// Conflict returns a KubeError that matches ErrConflict.
func Conflict(format string, args ...any) error {
	return classified{&KubeError{fmt.Sprintf(format, args...)}, ErrConflict}
}

func kubeErr(format string, args ...any) error {
	return &KubeError{fmt.Sprintf(format, args...)}
}

// Namespace is a namespace of the cluster.
type Namespace struct {
	Name     string
	UID      string
	Labels   map[string]string
	Created  time.Time // the API server's creationTimestamp
	Deleting bool
}

// Pod is a pod of a namespace.
type Pod struct {
	Name     string
	UID      string
	Labels   map[string]string
	Phase    string // Pending, Running, Succeeded, Failed, Unknown
	Ready    bool
	Deleting bool
	Node     string
	Restarts int
	// Waiting is a container's waiting reason, such as ImagePullBackOff.
	Waiting string
	// ExitCode is the first container's, once terminated; nil before.
	ExitCode *int
	// Reason is the first container's termination reason, such as OOMKilled.
	Reason string
}

// Workload is a Deployment or a StatefulSet.
type Workload struct {
	Kind       string // Deployment or StatefulSet
	Name       string
	Labels     map[string]string
	Selector   map[string]string // spec.selector.matchLabels
	Replicas   int
	Ready      int
	Available  int
	Updated    int
	Generation int
	Observed   int
}

// ExecResult is what a command run in a pod answered. A nonzero exit is a result, not an error.
type ExecResult struct {
	ExitCode int
	Output   string // standard output and standard error
}

// PodStats is one pod's counters from its node's kubelet summary.
type PodStats struct {
	CPUSeconds float64
	RxBytes    int64
	TxBytes    int64
}

// KubeAPI is what the deploy layer does to a cluster. Every call has a deadline of its
// implementation; every failure is a *KubeError (ErrNotFound and ErrConflict classify some) or the
// context's error.
type KubeAPI interface {
	// CreateNamespace creates a namespace with its labels in one request.
	CreateNamespace(ctx context.Context, name string, labels map[string]string) (Namespace, error)
	// Namespaces lists the namespaces whose labels include every pair of selector.
	Namespaces(ctx context.Context, selector map[string]string) ([]Namespace, error)
	// DeleteNamespace asks for deletion of the namespace if it still has this UID, without
	// waiting for it. ErrConflict when it has another UID, ErrNotFound when gone.
	DeleteNamespace(ctx context.Context, name, uid string) error
	// Apply is a server-side apply of the objects into the namespace.
	Apply(ctx context.Context, namespace string, objects []Object) error
	// Workloads lists the Deployments and StatefulSets of the namespace.
	Workloads(ctx context.Context, namespace string) ([]Workload, error)
	// Pods lists the pods of the namespace (whose labels include selector, when given).
	Pods(ctx context.Context, namespace string, selector map[string]string) ([]Pod, error)
	// DeletePods deletes the pods; one already gone is not an error. It does not wait.
	DeletePods(ctx context.Context, namespace string, names []string, graceSecs int) error
	// Scale sets a Deployment's replica count.
	Scale(ctx context.Context, namespace, deployment string, replicas int) error
	// Exec runs argv in the pod's first container.
	Exec(ctx context.Context, namespace, pod string, argv []string, timeout time.Duration) (ExecResult, error)
	// PutFiles writes files into a directory of the running pod, in the order given.
	PutFiles(ctx context.Context, namespace, pod, directory string, files []File) error
	// GetFiles copies every file of the pod's directory into the local directory into and
	// returns their relative names, sorted.
	GetFiles(ctx context.Context, namespace, pod, directory, into string) ([]string, error)
	// Logs is the pod's log (its previous container's with previous); "" when there is none.
	Logs(ctx context.Context, namespace, pod string, tail int, previous bool) (string, error)
	// PodStats is the kubelet counters of the namespace's pods on a node, by pod name.
	PodStats(ctx context.Context, namespace, node string) (map[string]PodStats, error)
}

// File is one file of a PutFiles call.
type File struct {
	Name string
	Data []byte
}

// Object is one Kubernetes object, as the JSON a manifest document decodes to.
type Object = map[string]any
