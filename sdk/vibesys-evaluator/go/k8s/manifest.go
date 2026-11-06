package k8s

import (
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// ManifestError means the manifest cannot be applied as written; the message names the file,
// object or line.
type ManifestError struct{ Message string }

func (e *ManifestError) Error() string { return e.Message }

func manifestErr(format string, args ...any) error {
	return &ManifestError{fmt.Sprintf(format, args...)}
}

// AllowedKinds are the namespaced kinds an application may bring, as {API group, kind}. Anything
// else, in particular every cluster-scoped kind (Namespace, ClusterRole, PersistentVolume, CRDs),
// RBAC (Role, RoleBinding) and node-wide workloads (DaemonSet), is refused: the cluster is
// shared, and a namespace deletion must remove everything a sample made.
var AllowedKinds = [][2]string{
	{"", "ConfigMap"},
	{"", "Secret"},
	{"", "Service"},
	{"", "ServiceAccount"},
	{"", "PersistentVolumeClaim"},
	{"apps", "Deployment"},
	{"apps", "StatefulSet"},
	{"policy", "PodDisruptionBudget"},
}

func isWorkloadKind(kind string) bool { return kind == "Deployment" || kind == "StatefulSet" }

var variable = regexp.MustCompile(`\$\{([^}]*)\}`)

// Rendered is the manifest's objects for one namespace. First is applied and waited for before the
// seed, Rest after it (empty without a data tier).
type Rendered struct {
	First []Object
	Rest  []Object
	Text  string
}

// Objects are First followed by Rest.
func (r Rendered) Objects() []Object { return append(append([]Object{}, r.First...), r.Rest...) }

// Substitute replaces each ${NAME} by values[NAME]; any other ${...} is an error naming its line.
func Substitute(text string, values map[string]string, source string) (string, error) {
	var out strings.Builder
	last := 0
	for _, loc := range variable.FindAllStringSubmatchIndex(text, -1) {
		name := text[loc[2]:loc[3]]
		value, ok := values[name]
		if !ok {
			known := make([]string, 0, len(values))
			for k := range values {
				known = append(known, k)
			}
			sort.Strings(known)
			return "", manifestErr("%s:%d: unknown variable ${%s} (known: %s)",
				source, strings.Count(text[:loc[0]], "\n")+1, name, strings.Join(known, ", "))
		}
		out.WriteString(text[last:loc[0]])
		out.WriteString(value)
		last = loc[1]
	}
	out.WriteString(text[last:])
	return out.String(), nil
}

func identity(obj Object, index int, source string) (group, kind, name string, err error) {
	apiVersion, _ := obj["apiVersion"].(string)
	kind, _ = obj["kind"].(string)
	metadata, _ := obj["metadata"].(map[string]any)
	name, _ = metadata["name"].(string)
	if apiVersion == "" || kind == "" || name == "" {
		return "", "", "", manifestErr("%s: document %d lacks apiVersion, kind or metadata.name", source, index+1)
	}
	if i := strings.LastIndex(apiVersion, "/"); i >= 0 {
		group = apiVersion[:i]
	}
	return group, kind, name, nil
}

// Render substitutes ${NAMESPACE} and the images, parses, checks, sets the namespace and splits.
//
// It returns a *ManifestError for an unknown variable, an image variable the manifest does not
// use, a kind outside AllowedKinds, an object of another namespace, a duplicate object, or a data
// tier that selects no workload.
func Render(text, namespace string, images, dataTier map[string]string, source string) (Rendered, error) {
	if source == "" {
		source = "manifest"
	}
	names := make([]string, 0, len(images))
	for v := range images {
		names = append(names, v)
	}
	sort.Strings(names)
	for _, v := range names {
		if !strings.Contains(text, "${"+v+"}") {
			return Rendered{}, manifestErr("%s: images[%s] is not used (no ${%s})", source, pyq(v), v)
		}
	}
	values := map[string]string{"NAMESPACE": namespace}
	for k, v := range images {
		values[k] = v
	}
	rendered, err := Substitute(text, values, source)
	if err != nil {
		return Rendered{}, err
	}
	var documents []Object
	decoder := yaml.NewDecoder(strings.NewReader(rendered))
	for index := 0; ; index++ {
		var doc any
		if err := decoder.Decode(&doc); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			return Rendered{}, manifestErr("%s: not YAML: %v", source, err)
		}
		if doc == nil {
			index--
			continue
		}
		obj, ok := doc.(map[string]any)
		if !ok {
			return Rendered{}, manifestErr("%s: document %d is not a mapping", source, len(documents)+1)
		}
		documents = append(documents, obj)
	}
	seen := map[[2]string]bool{}
	var result Rendered
	result.Text = rendered
	for index, obj := range documents {
		group, kind, name, err := identity(obj, index, source)
		if err != nil {
			return Rendered{}, err
		}
		where := fmt.Sprintf("%s: %s %s", source, kind, name)
		allowed := false
		for _, k := range AllowedKinds {
			allowed = allowed || (k[0] == group && k[1] == kind)
		}
		if !allowed {
			kinds := make([]string, 0, len(AllowedKinds))
			for _, k := range AllowedKinds {
				kinds = append(kinds, k[1])
			}
			sort.Strings(kinds)
			return Rendered{}, manifestErr("%s: kind not allowed (allowed: %s)", where, strings.Join(kinds, ", "))
		}
		metadata := obj["metadata"].(map[string]any)
		if ns, present := metadata["namespace"]; present && ns != namespace {
			return Rendered{}, manifestErr("%s: metadata.namespace must be ${NAMESPACE} or absent", where)
		}
		metadata["namespace"] = namespace
		key := [2]string{kind, name}
		if seen[key] {
			return Rendered{}, manifestErr("%s: defined twice", where)
		}
		seen[key] = true
		labels, _ := metadata["labels"].(map[string]any)
		inTier := !isWorkloadKind(kind) || (len(dataTier) > 0 && labelsMatch(dataTier, labels))
		if inTier || len(dataTier) == 0 {
			result.First = append(result.First, obj)
		} else {
			result.Rest = append(result.Rest, obj)
		}
	}
	if len(dataTier) > 0 {
		found := false
		for _, obj := range result.First {
			found = found || isWorkloadKind(obj["kind"].(string))
		}
		if !found {
			return Rendered{}, manifestErr("%s: data_tier %q selects no Deployment or StatefulSet (it matches metadata.labels)",
				source, selectorString(dataTier))
		}
	}
	return result, nil
}

func labelsMatch(selector map[string]string, labels map[string]any) bool {
	for k, v := range selector {
		if got, _ := labels[k].(string); got != v {
			return false
		}
	}
	return true
}

func selectorString(labels map[string]string) string {
	pairs := make([]string, 0, len(labels))
	for k, v := range labels {
		pairs = append(pairs, k+"="+v)
	}
	sort.Strings(pairs)
	return strings.Join(pairs, ",")
}

// workloadNames are the names of the objects of the kind.
func workloadNames(objects []Object, kind string) map[string]bool {
	names := map[string]bool{}
	for _, obj := range objects {
		if obj["kind"] == kind {
			names[obj["metadata"].(map[string]any)["name"].(string)] = true
		}
	}
	return names
}

func objectName(obj Object) string { return obj["metadata"].(map[string]any)["name"].(string) }
