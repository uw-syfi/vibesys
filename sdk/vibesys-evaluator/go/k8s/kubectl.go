package k8s

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	fieldManager = "vibesys-evaluator"
	callTimeout  = 60 * time.Second
	applyTimeout = 180 * time.Second
	copyTimeout  = 600 * time.Second
)

// Kubectl is the KubeAPI over the kubectl binary and the kubeconfig in the environment. Each call
// is one kubectl process with a deadline; failures are *KubeError with kubectl's stderr.
type Kubectl struct {
	// Binary is kubectl; "kubectl" when empty.
	Binary string
	// Env is the environment of the processes; the current one when nil.
	Env []string
}

var _ KubeAPI = Kubectl{}

func (k Kubectl) binary() string {
	if k.Binary != "" {
		return k.Binary
	}
	return "kubectl"
}

type run struct {
	stdout, stderr []byte
	code           int
}

func (k Kubectl) run(ctx context.Context, args []string, stdin []byte, timeout time.Duration, check bool) (run, error) {
	ctx2, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx2, k.binary(), args...)
	cmd.Env = k.Env
	if stdin != nil {
		cmd.Stdin = bytes.NewReader(stdin)
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	out := run{stdout: stdout.Bytes(), stderr: stderr.Bytes()}
	var exit *exec.ExitError
	switch {
	case err == nil:
	case errors.As(err, &exit) && ctx2.Err() == nil:
		out.code = exit.ExitCode()
	case ctx.Err() != nil:
		return out, ctx.Err()
	case ctx2.Err() != nil:
		return out, kubeErr("kubectl %s: no answer in %.0fs", strings.Join(head(args, 3), " "), timeout.Seconds())
	default:
		return out, kubeErr("kubectl %s: %v", strings.Join(head(args, 3), " "), err)
	}
	if check && out.code != 0 {
		message := strings.TrimSpace(string(lastBytes(out.stderr, 2000)))
		switch {
		case strings.Contains(message, "(NotFound)"):
			return out, NotFound("%s", message)
		case strings.Contains(message, "(Conflict)") || strings.Contains(message, "Precondition failed"):
			return out, Conflict("%s", message)
		}
		return out, kubeErr("kubectl %s failed (%d): %s", strings.Join(head(args, 4), " "), out.code, message)
	}
	return out, nil
}

func head(s []string, n int) []string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

func lastBytes(b []byte, n int) []byte {
	if len(b) > n {
		return b[len(b)-n:]
	}
	return b
}

func (k Kubectl) json(ctx context.Context, args []string, stdin []byte, into any) error {
	out, err := k.run(ctx, append(append([]string{}, args...), "-o", "json"), stdin, callTimeout, true)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(out.stdout, into); err != nil {
		return kubeErr("kubectl %s: not the JSON expected: %v", strings.Join(head(args, 3), " "), err)
	}
	return nil
}

func selectorArg(labels map[string]string) string {
	pairs := make([]string, 0, len(labels))
	for k, v := range labels {
		pairs = append(pairs, k+"="+v)
	}
	sort.Strings(pairs)
	return strings.Join(pairs, ",")
}

type rawMeta struct {
	Name              string            `json:"name"`
	UID               string            `json:"uid"`
	Labels            map[string]string `json:"labels"`
	CreationTimestamp string            `json:"creationTimestamp"`
	DeletionTimestamp *string           `json:"deletionTimestamp"`
	Generation        int               `json:"generation"`
}

func parseNamespace(meta rawMeta) (Namespace, error) {
	created, err := time.Parse(time.RFC3339, meta.CreationTimestamp)
	if err != nil {
		return Namespace{}, kubeErr("namespace %s: bad creationTimestamp %q", meta.Name, meta.CreationTimestamp)
	}
	return Namespace{Name: meta.Name, UID: meta.UID, Labels: nonNil(meta.Labels), Created: created, Deleting: meta.DeletionTimestamp != nil && *meta.DeletionTimestamp != ""}, nil
}

func nonNil(m map[string]string) map[string]string {
	if m == nil {
		return map[string]string{}
	}
	return m
}

type rawPod struct {
	Metadata rawMeta `json:"metadata"`
	Spec     struct {
		NodeName string `json:"nodeName"`
	} `json:"spec"`
	Status struct {
		Phase      string `json:"phase"`
		Conditions []struct {
			Type   string `json:"type"`
			Status string `json:"status"`
			Reason string `json:"reason"`
		} `json:"conditions"`
		ContainerStatuses []struct {
			RestartCount int `json:"restartCount"`
			State        struct {
				Waiting *struct {
					Reason string `json:"reason"`
				} `json:"waiting"`
				Terminated *struct {
					ExitCode int    `json:"exitCode"`
					Reason   string `json:"reason"`
				} `json:"terminated"`
			} `json:"state"`
		} `json:"containerStatuses"`
	} `json:"status"`
}

// ParsePod reads a pod from the JSON of `kubectl get pod -o json`.
func ParsePod(data []byte) (Pod, error) {
	var raw rawPod
	if err := json.Unmarshal(data, &raw); err != nil {
		return Pod{}, kubeErr("not a pod: %v", err)
	}
	return raw.pod(), nil
}

func (raw rawPod) pod() Pod {
	status := raw.Status
	conditions := map[string]string{}
	reasons := map[string]string{}
	for _, c := range status.Conditions {
		conditions[c.Type] = c.Status
		reasons[c.Type] = c.Reason
	}
	waiting := ""
	restarts := 0
	for _, s := range status.ContainerStatuses {
		restarts += s.RestartCount
		if waiting == "" && s.State.Waiting != nil {
			waiting = s.State.Waiting.Reason
		}
	}
	if waiting == "" && conditions["PodScheduled"] == "False" {
		waiting = reasons["PodScheduled"]
		if waiting == "" {
			waiting = "Unschedulable"
		}
	}
	pod := Pod{
		Name: raw.Metadata.Name, UID: raw.Metadata.UID, Labels: nonNil(raw.Metadata.Labels),
		Phase: status.Phase, Ready: conditions["Ready"] == "True",
		Deleting: raw.Metadata.DeletionTimestamp != nil && *raw.Metadata.DeletionTimestamp != "",
		Node:     raw.Spec.NodeName, Restarts: restarts, Waiting: waiting,
	}
	if pod.Phase == "" {
		pod.Phase = "Unknown"
	}
	if len(status.ContainerStatuses) > 0 {
		if t := status.ContainerStatuses[0].State.Terminated; t != nil {
			code := t.ExitCode
			pod.ExitCode, pod.Reason = &code, t.Reason
		}
	}
	return pod
}

// CreateNamespace implements KubeAPI.
func (k Kubectl) CreateNamespace(ctx context.Context, name string, labels map[string]string) (Namespace, error) {
	body, _ := json.Marshal(map[string]any{"apiVersion": "v1", "kind": "Namespace", "metadata": map[string]any{"name": name, "labels": labels}})
	var raw struct {
		Metadata rawMeta `json:"metadata"`
	}
	if err := k.json(ctx, []string{"create", "-f", "-"}, body, &raw); err != nil {
		return Namespace{}, err
	}
	return parseNamespace(raw.Metadata)
}

// Namespaces implements KubeAPI.
func (k Kubectl) Namespaces(ctx context.Context, selector map[string]string) ([]Namespace, error) {
	var list struct {
		Items []struct {
			Metadata rawMeta `json:"metadata"`
		} `json:"items"`
	}
	if err := k.json(ctx, []string{"get", "namespaces", "-l", selectorArg(selector)}, nil, &list); err != nil {
		return nil, err
	}
	var found []Namespace
	for _, item := range list.Items {
		ns, err := parseNamespace(item.Metadata)
		if err != nil {
			return nil, err
		}
		found = append(found, ns)
	}
	return found, nil
}

// DeleteNamespace implements KubeAPI.
func (k Kubectl) DeleteNamespace(ctx context.Context, name, uid string) error {
	body, _ := json.Marshal(map[string]any{
		"kind": "DeleteOptions", "apiVersion": "v1",
		"preconditions": map[string]any{"uid": uid}, "propagationPolicy": "Background",
	})
	file, err := os.CreateTemp("", "vse-delete-*.json")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err := file.Write(body); err != nil {
		return err
	}
	file.Close()
	_, err = k.run(ctx, []string{"delete", "--raw", "/api/v1/namespaces/" + name, "-f", file.Name()}, nil, callTimeout, true)
	return err
}

// Apply implements KubeAPI.
func (k Kubectl) Apply(ctx context.Context, namespace string, objects []Object) error {
	body, err := json.Marshal(map[string]any{"apiVersion": "v1", "kind": "List", "items": objects})
	if err != nil {
		return err
	}
	_, err = k.run(ctx, []string{"apply", "-n", namespace, "--server-side", "--force-conflicts", "--field-manager=" + fieldManager, "-f", "-"}, body, applyTimeout, true)
	return err
}

// Workloads implements KubeAPI.
func (k Kubectl) Workloads(ctx context.Context, namespace string) ([]Workload, error) {
	var list struct {
		Items []struct {
			Kind     string  `json:"kind"`
			Metadata rawMeta `json:"metadata"`
			Spec     struct {
				Replicas *int `json:"replicas"`
				Selector struct {
					MatchLabels map[string]string `json:"matchLabels"`
				} `json:"selector"`
			} `json:"spec"`
			Status struct {
				ReadyReplicas      int `json:"readyReplicas"`
				AvailableReplicas  int `json:"availableReplicas"`
				UpdatedReplicas    int `json:"updatedReplicas"`
				ObservedGeneration int `json:"observedGeneration"`
			} `json:"status"`
		} `json:"items"`
	}
	if err := k.json(ctx, []string{"get", "deployments.apps,statefulsets.apps", "-n", namespace}, nil, &list); err != nil {
		return nil, err
	}
	var found []Workload
	for _, item := range list.Items {
		replicas := 1
		if item.Spec.Replicas != nil {
			replicas = *item.Spec.Replicas
		}
		found = append(found, Workload{
			Kind: item.Kind, Name: item.Metadata.Name, Labels: nonNil(item.Metadata.Labels),
			Selector: nonNil(item.Spec.Selector.MatchLabels), Replicas: replicas,
			Ready: item.Status.ReadyReplicas, Available: item.Status.AvailableReplicas,
			Updated: item.Status.UpdatedReplicas, Generation: item.Metadata.Generation,
			Observed: item.Status.ObservedGeneration,
		})
	}
	return found, nil
}

// Pods implements KubeAPI.
func (k Kubectl) Pods(ctx context.Context, namespace string, selector map[string]string) ([]Pod, error) {
	args := []string{"get", "pods", "-n", namespace}
	if len(selector) > 0 {
		args = append(args, "-l", selectorArg(selector))
	}
	var list struct {
		Items []rawPod `json:"items"`
	}
	if err := k.json(ctx, args, nil, &list); err != nil {
		return nil, err
	}
	var found []Pod
	for _, item := range list.Items {
		found = append(found, item.pod())
	}
	return found, nil
}

// DeletePods implements KubeAPI.
func (k Kubectl) DeletePods(ctx context.Context, namespace string, names []string, graceSecs int) error {
	if len(names) == 0 {
		return nil
	}
	args := append([]string{"delete", "pod", "-n", namespace}, names...)
	args = append(args, fmt.Sprintf("--grace-period=%d", graceSecs), "--wait=false", "--ignore-not-found")
	_, err := k.run(ctx, args, nil, callTimeout, true)
	return err
}

// Scale implements KubeAPI.
func (k Kubectl) Scale(ctx context.Context, namespace, deployment string, replicas int) error {
	_, err := k.run(ctx, []string{"scale", "-n", namespace, "deployment/" + deployment, fmt.Sprintf("--replicas=%d", replicas)}, nil, callTimeout, true)
	return err
}

// Exec implements KubeAPI.
func (k Kubectl) Exec(ctx context.Context, namespace, pod string, argv []string, timeout time.Duration) (ExecResult, error) {
	out, err := k.run(ctx, append([]string{"exec", "-n", namespace, pod, "--"}, argv...), nil, timeout, false)
	if err != nil {
		return ExecResult{}, err
	}
	output := string(out.stdout) + string(out.stderr)
	if out.code != 0 && (strings.Contains(output, "Error from server") || strings.Contains(output, "error: unable to upgrade")) {
		return ExecResult{}, kubeErr("kubectl exec %s: %s", pod, lastString(strings.TrimSpace(output), 1500))
	}
	return ExecResult{ExitCode: out.code, Output: output}, nil
}

func lastString(s string, n int) string {
	if len(s) > n {
		return s[len(s)-n:]
	}
	return s
}

// PutFiles implements KubeAPI.
func (k Kubectl) PutFiles(ctx context.Context, namespace, pod, directory string, files []File) error {
	var buffer bytes.Buffer
	archive := tar.NewWriter(&buffer)
	for _, f := range files {
		if err := archive.WriteHeader(&tar.Header{Name: f.Name, Size: int64(len(f.Data)), Mode: 0o644, Typeflag: tar.TypeReg}); err != nil {
			return err
		}
		if _, err := archive.Write(f.Data); err != nil {
			return err
		}
	}
	if err := archive.Close(); err != nil {
		return err
	}
	_, err := k.run(ctx, []string{"exec", "-i", "-n", namespace, pod, "--", "tar", "-x", "-m", "-C", directory, "-f", "-"}, buffer.Bytes(), copyTimeout, true)
	return err
}

// GetFiles implements KubeAPI.
func (k Kubectl) GetFiles(ctx context.Context, namespace, pod, directory, into string) ([]string, error) {
	if err := os.MkdirAll(into, 0o755); err != nil {
		return nil, err
	}
	stream, err := os.CreateTemp("", "vse-get-*.tar")
	if err != nil {
		return nil, err
	}
	defer os.Remove(stream.Name())
	defer stream.Close()
	ctx2, cancel := context.WithTimeout(ctx, copyTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx2, k.binary(), "exec", "-n", namespace, pod, "--", "tar", "-c", "-C", directory, "-f", "-", ".")
	cmd.Env = k.Env
	var stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = stream, &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if ctx2.Err() != nil {
			return nil, kubeErr("copying %s out of %s: no end in %.0fs", directory, pod, copyTimeout.Seconds())
		}
		return nil, kubeErr("copying %s out of %s failed: %s", directory, pod, lastString(stderr.String(), 1500))
	}
	if _, err := stream.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	reader := tar.NewReader(stream)
	var names []string
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, kubeErr("copying %s out of %s: bad tar stream: %v", directory, pod, err)
		}
		name := filepath.Clean(strings.TrimPrefix(header.Name, "./"))
		if !filepath.IsLocal(name) {
			continue // "data" filter: never write outside into
		}
		target := filepath.Join(into, name)
		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return nil, err
			}
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return nil, err
			}
			f, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
			if err != nil {
				return nil, err
			}
			if _, err := io.Copy(f, reader); err != nil {
				f.Close()
				return nil, err
			}
			if err := f.Close(); err != nil {
				return nil, err
			}
			names = append(names, filepath.ToSlash(name))
		}
	}
	sort.Strings(names)
	return names, nil
}

// Logs implements KubeAPI.
func (k Kubectl) Logs(ctx context.Context, namespace, pod string, tail int, previous bool) (string, error) {
	args := []string{"logs", "-n", namespace, pod}
	if tail > 0 {
		args = append(args, fmt.Sprintf("--tail=%d", tail))
	}
	if previous {
		args = append(args, "--previous")
	}
	out, err := k.run(ctx, args, nil, copyTimeout, false)
	if err != nil {
		return "", err
	}
	if out.code != 0 {
		return "", nil
	}
	return string(out.stdout), nil
}

// PodStats implements KubeAPI.
func (k Kubectl) PodStats(ctx context.Context, namespace, node string) (map[string]PodStats, error) {
	out, err := k.run(ctx, []string{"get", "--raw", "/api/v1/nodes/" + node + "/proxy/stats/summary"}, nil, callTimeout, true)
	if err != nil {
		return nil, err
	}
	var summary struct {
		Pods []struct {
			PodRef struct {
				Name      string `json:"name"`
				Namespace string `json:"namespace"`
			} `json:"podRef"`
			CPU *struct {
				UsageCoreNanoSeconds float64 `json:"usageCoreNanoSeconds"`
			} `json:"cpu"`
			Network *struct {
				RxBytes int64 `json:"rxBytes"`
				TxBytes int64 `json:"txBytes"`
			} `json:"network"`
		} `json:"pods"`
	}
	if err := json.Unmarshal(out.stdout, &summary); err != nil {
		return nil, kubeErr("kubelet stats of %s: %v", node, err)
	}
	stats := map[string]PodStats{}
	for _, p := range summary.Pods {
		if p.PodRef.Namespace != namespace {
			continue
		}
		s := PodStats{}
		if p.CPU != nil {
			s.CPUSeconds = p.CPU.UsageCoreNanoSeconds / 1e9
		}
		if p.Network != nil {
			s.RxBytes, s.TxBytes = p.Network.RxBytes, p.Network.TxBytes
		}
		stats[p.PodRef.Name] = s
	}
	return stats, nil
}
