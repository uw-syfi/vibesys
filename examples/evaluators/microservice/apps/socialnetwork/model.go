package socialnetwork

import "sync"

type benchmarkUser struct {
	mu       sync.RWMutex
	id       int
	username string
	followee int
	follower int
	expected []expectedPost
}

type expectedPost struct {
	text      string
	postID    string
	requestID string
	timestamp string
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
	expected     []expectedPost
	committed    bool
	release      func()
}
