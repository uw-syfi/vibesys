package socialnetwork

import "sync"

type benchmarkUser struct {
	mu       sync.RWMutex
	id       int
	username string
	followee int
}

type dataset struct {
	namespace string
	users     []benchmarkUser
}

type operationState struct {
	kind         string
	user         *benchmarkUser
	marker       string
	timelineSize int
	release      func()
}
