package k8s

import (
	"fmt"
	"net/url"
	"path"
	"regexp"
	"slices"
	"strconv"
	"strings"
)

// CacheKind is the protocol of a cache Deployment, which says how its contents are flushed.
type CacheKind string

// The caches a task may flush in place.
const (
	Memcached CacheKind = "memcached"
	Redis     CacheKind = "redis"
)

// App is an application on Kubernetes: what to build, what to apply, how to tell it serves. It
// is the Go form of Python's `microservices.k8s.App`; the fields mean the same.
type App struct {
	// Name is the task's application name (lowercase letters, digits and dashes). It labels the
	// namespaces of the task, so the sweep of stale namespaces only ever deletes this task's.
	Name string
	// Manifest is the manifest's path relative to the task directory. ${NAMESPACE} and every
	// key of Images are substituted; any other ${...} is an error. Only namespaced application
	// kinds are accepted (see AllowedKinds).
	Manifest string
	// Images maps a manifest variable to a candidate directory (relative to the project root),
	// each built with its own Dockerfile and tagged by a digest of the directory.
	Images map[string]string
	// Gateway is the base URL of every request, http://<service>:<port>, a Service of the manifest.
	Gateway string
	// DataTier is a label selector (key=value[,key=value]). The objects it selects and every
	// non-workload object are applied first and waited for; then the seed runs; then the
	// remaining workloads are applied. Empty: the whole manifest serves before the seed runs.
	DataTier string
	// HasSeed says the deploy runs a seed pod (Deploy is given its command).
	HasSeed bool
	// Restartable are the Deployments the fault actions may restart, exec in, stop and start.
	Restartable []string
	// Caches are the cache Deployments the fault actions may empty in place, with their protocol.
	// They may also be restarted and exec'd in.
	Caches map[string]CacheKind
}

var (
	namePattern     = regexp.MustCompile(`^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$`)
	dnsLabel        = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
	variablePattern = regexp.MustCompile(`^[A-Z][A-Z0-9_]*$`)
	labelKey        = regexp.MustCompile(`^([a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?/)?[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$`)
	labelValue      = regexp.MustCompile(`^([A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)?$`)
)

// Gateway is a gateway URL, split: the Service it names and its port.
type Gateway struct {
	URL     string
	Service string
	Port    int
}

// ParseGateway splits http://<service>:<port> (port 80 when absent) with no path, query or
// credentials.
func ParseGateway(rawURL string) (Gateway, error) {
	bad := func(format string, args ...any) error {
		return fmt.Errorf("k8s.App(gateway=%q): %s", rawURL, fmt.Sprintf(format, args...))
	}
	parts, err := url.Parse(rawURL)
	if err != nil {
		return Gateway{}, bad("%v", err)
	}
	if parts.Scheme != "http" || (parts.Path != "" && parts.Path != "/") || parts.RawQuery != "" ||
		parts.ForceQuery || parts.User != nil || parts.Fragment != "" {
		return Gateway{}, bad("want http://<service>:<port>, no path or query")
	}
	host := strings.ToLower(parts.Hostname())
	if !dnsLabel.MatchString(host) {
		return Gateway{}, bad("%q is not a Service name of the manifest", host)
	}
	port := 80
	if p := parts.Port(); p != "" {
		port, err = strconv.Atoi(p)
		if err != nil || port < 0 || port > 65535 {
			return Gateway{}, bad("port out of range: %s", p)
		}
	}
	return Gateway{URL: strings.TrimRight(rawURL, "/"), Service: host, Port: port}, nil
}

// ParseSelector reads an equality label selector key=value[,key=value] (== too); empty is nil.
func ParseSelector(selector string) (map[string]string, error) {
	var labels map[string]string
	for _, item := range strings.Split(selector, ",") {
		raw := strings.TrimSpace(item)
		if raw == "" {
			continue
		}
		key, value, found := strings.Cut(strings.ReplaceAll(raw, "==", "="), "=")
		key, value = strings.TrimSpace(key), strings.TrimSpace(value)
		if !found || strings.HasSuffix(key, "!") || !labelKey.MatchString(key) || !labelValue.MatchString(value) {
			return nil, fmt.Errorf("k8s.App(data_tier=%q): %q is not key=value (only equality selectors are supported)", selector, raw)
		}
		if labels == nil {
			labels = map[string]string{}
		}
		labels[key] = value
	}
	return labels, nil
}

func relativePath(field, value string) error {
	clean := path.Clean(value)
	if value == "" || strings.HasPrefix(value, "/") || slices.Contains(strings.Split(value, "/"), "..") || clean == ".." {
		return fmt.Errorf("k8s.App(%s=%s): want a relative path without '..'", field, pyq(value))
	}
	return nil
}

// Validate returns an error naming the first field of the App that is wrong, and what to write.
func (a App) Validate() error {
	if !namePattern.MatchString(a.Name) {
		return fmt.Errorf("k8s.App(name=%s): want 1-40 lowercase letters, digits and dashes", pyq(a.Name))
	}
	if err := relativePath("manifest", a.Manifest); err != nil {
		return err
	}
	for variable, directory := range sortedPairs(a.Images) {
		if !variablePattern.MatchString(variable) || variable == "NAMESPACE" {
			return fmt.Errorf("k8s.App(images): key %s must be an UPPER_CASE manifest variable other than NAMESPACE", pyq(variable))
		}
		if err := relativePath(fmt.Sprintf("images[%s]", pyq(variable)), directory); err != nil {
			return err
		}
	}
	if _, err := ParseGateway(a.Gateway); err != nil {
		return err
	}
	tier, err := ParseSelector(a.DataTier)
	if err != nil {
		return err
	}
	if len(tier) > 0 && !a.HasSeed {
		return fmt.Errorf("k8s.App(data_tier=%q) orders the deploy around seed, but seed is None: remove data_tier or give seed", a.DataTier)
	}
	for _, deployment := range a.Restartable {
		if !dnsLabel.MatchString(deployment) {
			return fmt.Errorf("k8s.App(restartable): %q is not a Deployment name", deployment)
		}
	}
	seen := map[string]bool{}
	for _, deployment := range a.Restartable {
		if seen[deployment] {
			return fmt.Errorf("k8s.App(restartable): names repeat")
		}
		seen[deployment] = true
	}
	for cache, kind := range sortedPairs(a.Caches) {
		if !dnsLabel.MatchString(cache) {
			return fmt.Errorf("k8s.App(caches): %q is not a Deployment name", cache)
		}
		if kind != Memcached && kind != Redis {
			return fmt.Errorf("k8s.App(caches[%s]=%s): want one of memcached, redis", pyq(cache), pyq(string(kind)))
		}
	}
	return nil
}

// sortedPairs ranges over a map in key order, so the first error is deterministic.
func sortedPairs[V any](m map[string]V) func(yield func(string, V) bool) {
	return func(yield func(string, V) bool) {
		keys := make([]string, 0, len(m))
		for k := range m {
			keys = append(keys, k)
		}
		slices.Sort(keys)
		for _, k := range keys {
			if !yield(k, m[k]) {
				return
			}
		}
	}
}

// pyq quotes like Python's repr, so the messages of both front ends read alike.
func pyq(s string) string { return "'" + s + "'" }
