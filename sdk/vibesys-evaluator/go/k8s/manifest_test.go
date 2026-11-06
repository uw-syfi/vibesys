package k8s_test

import (
	"fmt"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
)

var images = map[string]string{"APP_IMAGE": "registry:5000/vse/shop/app:0123456789abcdef"}

func kinds(objects []k8s.Object) []string {
	var out []string
	for _, o := range objects {
		out = append(out, o["kind"].(string)+"/"+o["metadata"].(map[string]any)["name"].(string))
	}
	return out
}

func TestVariablesAreSubstitutedAndEveryObjectGetsTheNamespace(t *testing.T) {
	r, err := k8s.Render(manifest, "hk8s-shop-1", images, nil, "")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(r.Text, "${") {
		t.Fatal("variables left")
	}
	for _, o := range r.Objects() {
		if o["metadata"].(map[string]any)["namespace"] != "hk8s-shop-1" {
			t.Fatalf("%v", o)
		}
	}
	for _, o := range r.Objects() {
		if o["kind"] == "ConfigMap" && o["data"].(map[string]any)["db"] != "db.hk8s-shop-1.svc.cluster.local:27017" {
			t.Fatalf("%v", o)
		}
	}
}

func TestAnUnknownVariableIsNamedWithItsLine(t *testing.T) {
	_, err := k8s.Render(strings.Replace(manifest, "db.${NAMESPACE}", "${DB_IMAGE}", 1), "n", images, nil, "")
	contains(t, err, "manifest:6: unknown variable ${DB_IMAGE}")
}

func TestAnImageVariableTheManifestDoesNotUseIsRefused(t *testing.T) {
	_, err := k8s.Render(manifest, "n", map[string]string{"APP_IMAGE": "x", "OTHER_IMAGE": "x"}, nil, "")
	contains(t, err, "images['OTHER_IMAGE'] is not used")
}

func TestAnObjectOfAnotherNamespaceIsRefused(t *testing.T) {
	_, err := k8s.Render(strings.Replace(manifest, `namespace: "${NAMESPACE}"`, "namespace: default", 1), "n", images, nil, "")
	contains(t, err, "Service frontend: metadata.namespace")
}

func TestKindsOutsideTheAllowlistAreRefused(t *testing.T) {
	for _, c := range [][2]string{
		{"v1", "Namespace"}, {"rbac.authorization.k8s.io/v1", "ClusterRole"}, {"rbac.authorization.k8s.io/v1", "Role"},
		{"apps/v1", "DaemonSet"}, {"v1", "PersistentVolume"}, {"v1", "Pod"}, {"batch/v1", "Job"},
		{"extensions/v1", "Deployment"},
	} {
		text := fmt.Sprintf("apiVersion: %s\nkind: %s\nmetadata: {name: x}\n", c[0], c[1])
		_, err := k8s.Render(text, "n", nil, nil, "")
		contains(t, err, "kind not allowed")
	}
	for _, c := range k8s.AllowedKinds {
		version := "v1"
		if c[0] != "" {
			version = c[0] + "/v1"
		}
		text := fmt.Sprintf("apiVersion: %s\nkind: %s\nmetadata: {name: x}\n", version, c[1])
		if _, err := k8s.Render(text, "n", nil, nil, ""); err != nil {
			t.Fatalf("%v: %v", c, err)
		}
	}
}

func TestDuplicatesAndMalformedDocumentsAreRefused(t *testing.T) {
	twice := "apiVersion: v1\nkind: Service\nmetadata: {name: x}\n---\napiVersion: v1\nkind: Service\nmetadata: {name: x}\n"
	_, err := k8s.Render(twice, "n", nil, nil, "")
	contains(t, err, "Service x: defined twice")
	_, err = k8s.Render("apiVersion: v1\nkind: Service\n", "n", nil, nil, "")
	contains(t, err, "lacks apiVersion, kind or metadata.name")
	_, err = k8s.Render("- a\n- b\n", "n", nil, nil, "")
	contains(t, err, "not a mapping")
	_, err = k8s.Render("a: [\n", "n", nil, nil, "")
	contains(t, err, "not YAML")
}

func TestTheDataTierSplitsTheWorkloads(t *testing.T) {
	r, err := k8s.Render(manifest, "n", images, map[string]string{"tier": "data"}, "")
	if err != nil {
		t.Fatal(err)
	}
	if got := kinds(r.First); strings.Join(got, ",") != "ConfigMap/config,Service/db,Deployment/db,Service/frontend" {
		t.Fatalf("first %v", got)
	}
	if got := kinds(r.Rest); strings.Join(got, ",") != "Deployment/cache,Deployment/frontend,Deployment/backend" {
		t.Fatalf("rest %v", got)
	}
	_, err = k8s.Render(manifest, "n", images, map[string]string{"tier": "nosuch"}, "")
	contains(t, err, "selects no Deployment or StatefulSet")
}
