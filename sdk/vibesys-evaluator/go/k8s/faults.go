package k8s

import (
	"context"
	"sort"
	"strings"
	"time"
)

// Fault actions: what an accuracy check does to the running application between its phases.
// They act on the Deployments the App lists as Restartable or Caches, and nothing else: which
// targets to hit is task configuration.

// memcachedFlush is the command that empties a memcached in place (bash's /dev/tcp: the image
// needs no client).
const memcachedFlush = "exec 3<>/dev/tcp/127.0.0.1/11211; printf 'flush_all\\r\\n' >&3; " +
	`read -r reply <&3; echo "$reply"`

// ReplacedPod is a pod a restart deleted.
type ReplacedPod struct {
	Deployment string
	Pod        string
	UID        string
}

// RestartOptions say which pods Restart deletes and whether it waits.
type RestartOptions struct {
	// EveryPod deletes every pod of each Deployment at once; otherwise one (the first by name).
	EveryPod bool
	// NoWait returns once the pods are deleted: the caller waits with WaitReady, naming the
	// replaced UIDs.
	NoWait bool
}

// WaitOptions say what WaitReady waits for.
type WaitOptions struct {
	// Deployments to wait for; every workload of the namespace when empty.
	Deployments []string
	// Replaced are the UIDs of pods that must be gone (a restart's, see ReplacedPod.UID).
	Replaced []string
	// Timeout is the App's Wait() when zero.
	Timeout time.Duration
	// NoProbe skips the probes through the gateway that follow readiness.
	NoProbe bool
}

// PodExec is the answer of one pod to Exec.
type PodExec struct {
	Pod string
	ExecResult
}

// ExecTarget names where Exec runs: a Pod, or a Deployment (its first pod by name, or every live
// pod with AllPods).
type ExecTarget struct {
	Deployment string
	Pod        string
	AllPods    bool
}

func (d *Deployment) allowedNames() map[string]bool {
	allowed := map[string]bool{}
	for _, n := range d.App.Restartable {
		allowed[n] = true
	}
	for n := range d.App.Caches {
		allowed[n] = true
	}
	return allowed
}

func (d *Deployment) allowedList() string {
	names := sortedKeys(d.allowedNames())
	if len(names) == 0 {
		return "none"
	}
	return strings.Join(names, ", ")
}

func (d *Deployment) requireAllowed(verb, name string) error {
	if !d.allowedNames()[name] {
		return deployErr("%s %s: not in k8s.App(restartable) or caches (%s)", verb, name, d.allowedList())
	}
	return nil
}

func (d *Deployment) workloadPods(ctx context.Context, name string) ([]Pod, error) {
	workloads, err := d.kube().Workloads(ctx, d.Namespace)
	if err != nil {
		return nil, err
	}
	for _, w := range workloads {
		if w.Name == name {
			return d.kube().Pods(ctx, d.Namespace, w.Selector)
		}
	}
	return nil, deployErr("no Deployment %q in %s", name, d.Namespace)
}

// Restart deletes one pod (the first by name) or every pod of each Deployment at once. Unless
// opts.NoWait it returns when no deleted pod remains, every workload serves with all replicas and
// the probes pass.
func (d *Deployment) Restart(ctx context.Context, deployments []string, opts RestartOptions) ([]ReplacedPod, error) {
	for _, name := range deployments {
		if err := d.requireAllowed("restart", name); err != nil {
			return nil, err
		}
	}
	var replaced []ReplacedPod
	for _, name := range deployments {
		pods, err := d.workloadPods(ctx, name)
		if err != nil {
			return replaced, err
		}
		chosen := Victims(pods, opts.EveryPod)
		names := make([]string, len(chosen))
		for i, p := range chosen {
			names[i] = p.Name
			replaced = append(replaced, ReplacedPod{Deployment: name, Pod: p.Name, UID: p.UID})
		}
		if err := d.kube().DeletePods(ctx, d.Namespace, names, 1); err != nil {
			return replaced, err
		}
		d.log("restart: deleted %s", strings.Join(names, ", "))
	}
	if opts.NoWait {
		return replaced, nil
	}
	uids := make([]string, len(replaced))
	for i, r := range replaced {
		uids[i] = r.UID
	}
	return replaced, d.WaitReady(ctx, WaitOptions{Replaced: uids})
}

// WaitReady waits until every workload (of opts.Deployments) serves with all its replicas, none of
// them a replaced pod, then probes through the gateway.
func (d *Deployment) WaitReady(ctx context.Context, opts WaitOptions) error {
	var names map[string]bool
	if len(opts.Deployments) > 0 {
		names = map[string]bool{}
		for _, n := range opts.Deployments {
			names[n] = true
		}
	}
	replaced := map[string]bool{}
	for _, uid := range opts.Replaced {
		replaced[uid] = true
	}
	timeout := opts.Timeout
	if timeout == 0 {
		timeout = d.App.Wait()
	}
	if err := d.waitServing(ctx, names, replaced, timeout); err != nil {
		return err
	}
	if opts.NoProbe {
		return nil
	}
	return d.Probe(ctx)
}

// Exec runs argv in a pod, or in the pods of a Deployment, and returns every pod's answer; a
// nonzero exit is a result, not an error. The Deployment must be one of the App's Restartable or
// Caches.
func (d *Deployment) Exec(ctx context.Context, target ExecTarget, argv []string, timeout time.Duration) ([]PodExec, error) {
	if len(argv) == 0 {
		return nil, deployErr("exec: no command")
	}
	if (target.Pod == "") == (target.Deployment == "") {
		return nil, deployErr("exec: name a Deployment or a pod, not both or neither")
	}
	var pods []Pod
	if target.Deployment != "" {
		if err := d.requireAllowed("exec", target.Deployment); err != nil {
			return nil, err
		}
		found, err := d.workloadPods(ctx, target.Deployment)
		if err != nil {
			return nil, err
		}
		pods = Victims(found, target.AllPods)
		if len(pods) == 0 {
			return nil, deployErr("exec: Deployment %s has no live pod", target.Deployment)
		}
	} else {
		all, err := d.kube().Pods(ctx, d.Namespace, nil)
		if err != nil {
			return nil, err
		}
		workloads, err := d.kube().Workloads(ctx, d.Namespace)
		if err != nil {
			return nil, err
		}
		for _, p := range all {
			if p.Name != target.Pod {
				continue
			}
			for _, w := range workloads {
				if selects(w.Selector, p.Labels) && d.allowedNames()[w.Name] {
					pods = []Pod{p}
				}
			}
		}
		if pods == nil {
			return nil, deployErr("exec %s: not a pod of a Deployment in k8s.App(restartable) or caches (%s)", target.Pod, d.allowedList())
		}
	}
	if timeout == 0 {
		timeout = 60 * time.Second
	}
	var results []PodExec
	for _, p := range pods {
		result, err := d.kube().Exec(ctx, d.Namespace, p.Name, argv, timeout)
		if err != nil {
			return results, err
		}
		results = append(results, PodExec{Pod: p.Name, ExecResult: result})
	}
	return results, nil
}

// Flush empties a cache in place, on every pod of its Deployment, then probes.
func (d *Deployment) Flush(ctx context.Context, cache string) error {
	kind, ok := d.App.Caches[cache]
	if !ok {
		return deployErr("flush %s: not in k8s.App(caches) (%s)", cache, strings.Join(cacheNames(d.App), ", "))
	}
	argv := []string{"bash", "-c", memcachedFlush}
	if kind == Redis {
		argv = []string{"redis-cli", "FLUSHALL"}
	}
	pods, err := d.workloadPods(ctx, cache)
	if err != nil {
		return err
	}
	sort.Slice(pods, func(i, j int) bool { return pods[i].Name < pods[j].Name })
	for _, p := range pods {
		if p.Deleting {
			continue
		}
		result, err := d.kube().Exec(ctx, d.Namespace, p.Name, argv, 60*time.Second)
		if err != nil {
			return err
		}
		if result.ExitCode != 0 || !strings.Contains(result.Output, "OK") {
			return deployErr("flush %s (%s): %s", cache, p.Name, lastN(strings.TrimSpace(result.Output), 300))
		}
	}
	d.log("flushed %s", cache)
	return d.Probe(ctx)
}

// Stop scales every restartable Deployment to 0 and waits until their pods are gone.
func (d *Deployment) Stop(ctx context.Context) error {
	for _, name := range d.App.Restartable {
		if err := d.kube().Scale(ctx, d.Namespace, name, 0); err != nil {
			return err
		}
	}
	names := map[string]bool{}
	for _, n := range d.App.Restartable {
		names[n] = true
	}
	if err := d.waitServing(ctx, names, nil, d.App.Wait()); err != nil {
		return err
	}
	d.log("stopped %s", strings.Join(d.App.Restartable, ", "))
	return nil
}

// Start scales the restartable Deployments back to the manifest's replicas, waits and probes.
func (d *Deployment) Start(ctx context.Context) error {
	restartable := map[string]bool{}
	for _, n := range d.App.Restartable {
		restartable[n] = true
	}
	for _, obj := range d.rendered.Objects() {
		if obj["kind"] != "Deployment" || !restartable[objectName(obj)] {
			continue
		}
		replicas := 1
		if spec, ok := obj["spec"].(map[string]any); ok {
			switch r := spec["replicas"].(type) {
			case int:
				replicas = r
			case int64:
				replicas = int(r)
			case float64:
				replicas = int(r)
			}
		}
		if err := d.kube().Scale(ctx, d.Namespace, objectName(obj), replicas); err != nil {
			return err
		}
	}
	if err := d.waitServing(ctx, nil, nil, d.App.Wait()); err != nil {
		return err
	}
	return d.Probe(ctx)
}
