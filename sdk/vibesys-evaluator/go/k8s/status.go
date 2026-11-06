package k8s

import (
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// The pure decisions of the deploy layer: names, labels, the sweep, readiness, restart victims
// and usage.
//
// Namespaces of this package's tasks are hk8s-<slug>-<token>: the clusters it was built for keep
// per-task namespaces under this prefix. A namespace carries the task label (the App's name), its
// creation time and the vibesys-cli job that made it.
const (
	NamespacePrefix = "hk8s-"
	TaskLabel       = "vibesys.dev/evaluator-task"
	CreatedLabel    = "vibesys.dev/evaluator-created" // unix seconds
	JobLabel        = "vibesys.dev/evaluator-job"
	SweepMargin     = 600 * time.Second
	slugChars       = 24
)

// fatalWaiting are the waiting reasons a pod does not recover from by itself: fail at once instead
// of at the deadline.
var fatalWaiting = map[string]bool{"InvalidImageName": true, "CreateContainerConfigError": true, "ErrImageNeverPull": true}

// Slug is the App's name cut to fit a namespace name, without a trailing dash.
func Slug(appName string) string {
	if len(appName) > slugChars {
		appName = appName[:slugChars]
	}
	return strings.TrimRight(appName, "-")
}

// NamespacePrefixOf is the prefix of the namespaces of an App.
func NamespacePrefixOf(appName string) string { return NamespacePrefix + Slug(appName) + "-" }

// NamespaceName is the name of the namespace of an App with a token.
func NamespaceName(appName, token string) string { return NamespacePrefixOf(appName) + token }

// NamespaceLabels are the labels of a namespace this package makes.
func NamespaceLabels(appName string, now time.Time, job string) map[string]string {
	return map[string]string{TaskLabel: appName, CreatedLabel: strconv.FormatInt(now.Unix(), 10), JobLabel: job}
}

var (
	tokenPattern = regexp.MustCompile(`^[0-9a-z]{1,16}$`) // what follows the prefix in a name NamespaceName made
	jobPattern   = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$`)
)

// JobLabelOf is the vibesys-cli job id: the parent of the artifacts directory (.../<job>/<step>),
// else fallback.
func JobLabelOf(artifactsDir, fallback string) string {
	var parts []string
	for _, p := range strings.Split(artifactsDir, "/") {
		if p != "" {
			parts = append(parts, p)
		}
	}
	name := ""
	if len(parts) >= 2 {
		name = parts[len(parts)-2]
	}
	if jobPattern.MatchString(name) {
		return name
	}
	return fallback
}

func namespaceAge(ns Namespace, now time.Time) time.Duration {
	stamp := ns.Created
	if label := ns.Labels[CreatedLabel]; label != "" {
		if secs, err := strconv.ParseInt(label, 10, 64); err == nil && secs >= 0 {
			// The younger of the label's and the API server's age: neither our clock nor the
			// server's running ahead makes a namespace stale early.
			if t := time.Unix(secs, 0); t.After(stamp) {
				stamp = t
			}
		}
	}
	return now.Sub(stamp)
}

// Stale returns this task's namespaces that no live sample can own: labelled with its name, named
// with its prefix and a token, and older than ttl (the longest a job may run plus a margin).
func Stale(namespaces []Namespace, appName string, now time.Time, ttl time.Duration) []Namespace {
	prefix := NamespacePrefixOf(appName)
	var stale []Namespace
	for _, ns := range namespaces {
		if ns.Labels[TaskLabel] == appName && strings.HasPrefix(ns.Name, prefix) &&
			tokenPattern.MatchString(ns.Name[len(prefix):]) && !ns.Deleting && namespaceAge(ns, now) > ttl {
			stale = append(stale, ns)
		}
	}
	sort.Slice(stale, func(i, j int) bool { return stale[i].Name < stale[j].Name })
	return stale
}

func selects(selector, labels map[string]string) bool {
	if len(selector) == 0 {
		return false
	}
	for k, v := range selector {
		if got, ok := labels[k]; !ok || got != v {
			return false
		}
	}
	return true
}

func podReason(p Pod) string {
	switch {
	case p.Waiting != "":
		return p.Waiting
	case p.Deleting:
		return "terminating"
	case p.Phase != "Running":
		return p.Phase
	}
	return "not ready"
}

// Unready says why each workload (of names, when not nil) does not serve with all its replicas;
// nil when all do. A workload serves when its controller has seen its spec, exactly `replicas` of
// its pods exist that are neither terminating nor among the replaced UIDs, all of them Ready, and
// its status counts every replica ready, available and updated.
func Unready(workloads []Workload, pods []Pod, names map[string]bool, replaced map[string]bool) []string {
	byName := map[string]Workload{}
	for _, w := range workloads {
		byName[w.Name] = w
	}
	var reasons []string
	if names != nil {
		missing := []string{}
		for name := range names {
			if _, ok := byName[name]; !ok {
				missing = append(missing, name)
			}
		}
		sort.Strings(missing)
		for _, name := range missing {
			reasons = append(reasons, name+": no such Deployment or StatefulSet")
		}
	}
	sorted := append([]Workload{}, workloads...)
	sort.Slice(sorted, func(i, j int) bool {
		if sorted[i].Kind != sorted[j].Kind {
			return sorted[i].Kind < sorted[j].Kind
		}
		return sorted[i].Name < sorted[j].Name
	})
	for _, w := range sorted {
		if names != nil && !names[w.Name] {
			continue
		}
		var own, live, ready []Pod
		for _, p := range pods {
			if selects(w.Selector, p.Labels) {
				own = append(own, p)
			}
		}
		for _, p := range own {
			if !p.Deleting && !replaced[p.UID] {
				live = append(live, p)
			}
		}
		for _, p := range live {
			if p.Ready {
				ready = append(ready, p)
			}
		}
		if w.Observed >= w.Generation && len(live) == len(own) && len(own) == w.Replicas &&
			len(ready) == w.Replicas && w.Ready == w.Replicas && w.Available == w.Replicas && w.Updated == w.Replicas {
			continue
		}
		problems := map[string]bool{}
		for _, p := range own {
			if !(p.Ready && !p.Deleting && !replaced[p.UID]) {
				problems[p.Name+": "+podReason(p)] = true
			}
		}
		list := make([]string, 0, len(problems))
		for p := range problems {
			list = append(list, p)
		}
		sort.Strings(list)
		detail := ""
		if len(list) > 0 {
			detail = " (" + strings.Join(list, "; ") + ")"
		}
		reasons = append(reasons, fmt.Sprintf("%s/%s: %d/%d ready%s", strings.ToLower(w.Kind), w.Name, len(ready), w.Replicas, detail))
	}
	return reasons
}

// Fatal lists the pods whose waiting reason does not recover.
func Fatal(pods []Pod) []string {
	var found []string
	for _, p := range pods {
		if fatalWaiting[p.Waiting] {
			found = append(found, p.Name+": "+p.Waiting)
		}
	}
	sort.Strings(found)
	return found
}

// Victims are the pods a restart deletes: every live one, or the first by name.
func Victims(pods []Pod, every bool) []Pod {
	var live []Pod
	for _, p := range pods {
		if !p.Deleting {
			live = append(live, p)
		}
	}
	sort.Slice(live, func(i, j int) bool { return live[i].Name < live[j].Name })
	if every || len(live) == 0 {
		return live
	}
	return live[:1]
}

// Usage is the CPU seconds and network bytes of one pod since it started, and its workload.
type Usage struct {
	Workload   string  `json:"workload"`
	CPUSeconds float64 `json:"cpu_seconds"`
	RxBytes    int64   `json:"rx_bytes"`
	TxBytes    int64   `json:"tx_bytes"`
}

func round(x float64, places int) float64 {
	scale := 1.0
	for i := 0; i < places; i++ {
		scale *= 10
	}
	if x < 0 {
		return -float64(int64(-x*scale+0.5)) / scale
	}
	return float64(int64(x*scale+0.5)) / scale
}

// JoinUsage joins kubelet counters to the pods and their workloads, by pod name.
func JoinUsage(pods []Pod, workloads []Workload, stats map[string]PodStats) map[string]Usage {
	owner := map[string]string{}
	for _, p := range pods {
		for _, w := range workloads {
			if selects(w.Selector, p.Labels) {
				owner[p.Name] = w.Name
				break
			}
		}
	}
	out := map[string]Usage{}
	for name, s := range stats {
		out[name] = Usage{Workload: owner[name], CPUSeconds: round(s.CPUSeconds, 3), RxBytes: s.RxBytes, TxBytes: s.TxBytes}
	}
	return out
}

// UsageDelta is the CPU seconds each pod used between two snapshots (a pod new since before counts
// from 0).
func UsageDelta(before, after map[string]Usage) map[string]float64 {
	delta := map[string]float64{}
	for name, item := range after {
		delta[name] = round(item.CPUSeconds-before[name].CPUSeconds, 3)
	}
	return delta
}
