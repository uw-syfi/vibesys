package trainticket

import "fmt"

var servicePrefixes = map[string]string{
	"config":  "/api/v1/configservice",
	"station": "/api/v1/stationservice",
	"train":   "/api/v1/trainservice",
	"travel":  "/api/v1/travelservice",
	"route":   "/api/v1/routeservice",
	"price":   "/api/v1/priceservice",
}

var serviceOrder = []string{"config", "station", "train", "travel", "route", "price"}

var collectionSuffixes = map[string]string{
	"config": "/configs", "station": "/stations", "train": "/trains",
	"travel": "/trips", "route": "/routes", "price": "/prices",
}

func Services() []string { return append([]string(nil), serviceOrder...) }

func Path(service, suffix string) (string, error) {
	prefix, exists := servicePrefixes[service]
	if !exists {
		return "", fmt.Errorf("unknown Train Ticket service %q", service)
	}
	if suffix == "" || suffix[0] != '/' {
		return "", fmt.Errorf("Train Ticket service path suffix must start with '/': %q", suffix)
	}
	return prefix + suffix, nil
}

func MustPath(service, suffix string) string {
	path, err := Path(service, suffix)
	if err != nil {
		panic(err)
	}
	return path
}

func CollectionSuffix(service string) (string, error) {
	suffix, exists := collectionSuffixes[service]
	if !exists {
		return "", fmt.Errorf("unknown Train Ticket service %q", service)
	}
	return suffix, nil
}

func MustCollectionSuffix(service string) string {
	suffix, err := CollectionSuffix(service)
	if err != nil {
		panic(err)
	}
	return suffix
}

func MustCollectionPath(service string) string {
	return MustPath(service, MustCollectionSuffix(service))
}
