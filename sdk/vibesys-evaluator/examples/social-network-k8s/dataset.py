"""The social-network-k8s dataset: users, a follow graph and posts (spec example).

Replaces the Go module's `internal/dataset` (generator), `internal/seed`
(MongoDB and Redis documents) and run.py's create_indexes. Generation is a pure
function of SEED with one `Rand(SEED, kind, i)` stream per entity.

Shape (DefaultConfig of the Go generator): 20,000 users "user<id>"; a directed
follow graph with heavy-tailed follower counts (followees drawn by Zipf 0.75
over a random popularity order, 1 + Pareto2(12) per user, at most 2,000, 20%
followed back), about 294k edges; Pareto2(5) posts per user (at most 400),
about 91k posts over the 180 days before 2026-06-01, unique timestamps; home
timelines derived from the graph and the mentions (about 1.3M entries).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from vibesys_evaluator import Rand

SEED = 0x5EED50C1A1
USERS = 20_000
FOLLOWEE_SCALE, MAX_FOLLOWEES, RECIPROCITY = 12.0, 2_000, 0.2
POST_SCALE, MAX_POSTS = 5.0, 400
WINDOW_END_MS = 1_780_272_000_000  # 2026-06-01 UTC: seeded posts are older than any live one
WINDOW_MS = 180 * 24 * 3600 * 1000
FIRST_POST_ID = 1_000_000  # the services' ids are far above this range
SHORT_URL_PREFIX = "http://short-url/"
WORDS = ("time day way year week work home city road park river tree garden table chair "
         "window door book page music song game team ball field water coffee tea bread").split()


@dataclass(frozen=True)
class Post:
    id: int
    req_id: int
    author: int  # user index; the user's id is index + 1
    timestamp: int  # ms since the epoch
    text: str  # as stored: URLs already shortened
    mentions: tuple[int, ...]
    urls: tuple[tuple[str, str], ...]  # (shortened, expanded)
    media: tuple[tuple[str, str], ...]  # (id, type)


@dataclass(frozen=True)
class Dataset:
    usernames: tuple[str, ...]
    followees: tuple[tuple[int, ...], ...]  # sorted user indices
    followers: tuple[tuple[int, ...], ...]
    posts: tuple[Post, ...]  # by timestamp; posts[p].id == FIRST_POST_ID + p
    user_posts: tuple[tuple[int, ...], ...]  # post indices, newest first
    home: tuple[tuple[int, ...], ...]  # post indices, newest first
    popularity_rank: tuple[int, ...]  # most to least followed
    activity_rank: tuple[int, ...]  # most to least posting

    def follows(self, u: int, v: int) -> bool:
        row = self.followees[u]
        at = bisect.bisect_left(row, v)
        return at < len(row) and row[at] == v


def generate() -> Dataset:
    """The task's dataset (a few seconds); the seed Job and every phase pod call it."""
    order = list(range(USERS))
    shuffle(Rand(SEED, "popularity"), order)
    followees: list[set[int]] = [set() for _ in range(USERS)]
    edges = []
    for u in range(USERS):
        r = Rand(SEED, "followees", u)
        want = min(1 + pareto2(r, FOLLOWEE_SCALE), MAX_FOLLOWEES)
        for _ in range(4 * want):
            if len(followees[u]) >= want:
                break
            v = order[r.zipf(USERS, 0.75)]
            if v != u and v not in followees[u]:
                followees[u].add(v)
                edges.append((u, v))
    for index, (u, v) in enumerate(edges):
        if Rand(SEED, "reciprocity", index).random() < RECIPROCITY:
            followees[v].add(u)
    rows = tuple(tuple(sorted(f)) for f in followees)
    followers: list[list[int]] = [[] for _ in range(USERS)]
    for u, row in enumerate(rows):
        for v in row:
            followers[v].append(u)

    drafts = []
    counts = [0] * USERS
    for u in range(USERS):
        r = Rand(SEED, "posts", u)
        counts[u] = min(pareto2(r, POST_SCALE), MAX_POSTS)
        drafts += [(WINDOW_END_MS - WINDOW_MS + r.below(WINDOW_MS), u, k) for k in range(counts[u])]
    drafts.sort()
    posts = []
    previous = 0
    for index, (timestamp, author, k) in enumerate(drafts):
        timestamp = max(timestamp, previous + 1)  # unique: every timeline order is defined
        previous = timestamp
        posts.append(compose_post(rows, index, author, timestamp, Rand(SEED, f"post/{author}", k)))

    user_posts: list[list[int]] = [[] for _ in range(USERS)]
    home: list[list[int]] = [[] for _ in range(USERS)]
    for p in reversed(range(len(posts))):  # newest first
        post = posts[p]
        user_posts[post.author].append(p)
        for reader in {*followers[post.author], *post.mentions}:
            home[reader].append(p)
    return Dataset(
        usernames=tuple(f"user{u + 1}" for u in range(USERS)),
        followees=rows, followers=tuple(tuple(f) for f in followers), posts=tuple(posts),
        user_posts=tuple(map(tuple, user_posts)), home=tuple(map(tuple, home)),
        popularity_rank=tuple(sorted(range(USERS), key=lambda u: (-len(followers[u]), u))),
        activity_rank=tuple(sorted(range(USERS), key=lambda u: (-counts[u], u))),
    )


def compose_post(followees: tuple[tuple[int, ...], ...], index: int, author: int,
                 timestamp: int, r: Rand) -> Post:
    """One seeded post: words, mentions of followees, a URL at the end, media."""
    parts = [WORDS[r.below(len(WORDS))] for _ in range(min(3 + pareto2(r, 6), 30))]
    row = followees[author]
    mentions: list[int] = []
    if row and r.random() < 0.25:
        for _ in range(2 if len(row) > 1 and r.random() < 0.2 else 1):
            v = row[r.below(len(row))]
            if v not in mentions:
                mentions.append(v)
                parts.insert(r.below(len(parts) + 1), f"@user{v + 1}")
    urls = ()
    if r.random() < 0.15:  # the pinned text service drops text after the last URL
        urls = ((SHORT_URL_PREFIX + token(r, 10), f"http://example{r.below(10**6)}.com"),)
        parts.append(urls[0][0])
    media = ((str(10**16 + r.below(9 * 10**16)), "png"),) if r.random() < 0.1 else ()
    return Post(FIRST_POST_ID + index, r.below(1 << 52), author, timestamp, " ".join(parts),
                tuple(mentions), urls, media)


# -- documents the services themselves write (internal/seed) --

MONGO = {"user-mongodb": ("user", "user"), "social-graph-mongodb": ("social-graph", "social-graph"),
         "post-storage-mongodb": ("post", "post"), "user-timeline-mongodb": ("user-timeline", "user-timeline"),
         "url-shorten-mongodb": ("url-shorten", "url-shorten")}
INDEXES = {"user-mongodb": [("username", True), ("user_id", True)],
           "user-timeline-mongodb": [("user_id", True)]}


def mongo_documents(data: Dataset) -> dict[str, list[dict[str, object]]]:
    """Service name -> documents, field names and BSON types as the services write them."""
    return {
        "user-mongodb": [{"user_id": u + 1, "username": name, "first_name": f"first{u + 1}",
                          "last_name": f"last{u + 1}", "password": "...", "salt": "..."}
                         for u, name in enumerate(data.usernames)],
        "post-storage-mongodb": [{"post_id": p.id, "req_id": p.req_id, "creator": {
            "user_id": p.author + 1, "username": data.usernames[p.author]},
            "text": p.text, "timestamp": p.timestamp,
            "user_mentions": [{"user_id": v + 1, "username": data.usernames[v]} for v in p.mentions],
            "media": [{"media_id": int(i), "media_type": t} for i, t in p.media],
            "urls": [{"shortened_url": s, "expanded_url": e} for s, e in p.urls], "post_type": 0}
            for p in data.posts],
        # ... social-graph-mongodb, user-timeline-mongodb, url-shorten-mongodb likewise.
    }


def redis_entries(data: Dataset) -> dict[str, dict[str, list[tuple[float, str]]]]:
    """Service name -> sorted-set key -> (score, member): home timelines and the graph.

    home-timeline-redis is the only store of home timelines; social-graph-redis
    is filled too, because the pinned social-graph service treats an existing
    follower set as complete.
    """
    return {
        "home-timeline-redis": {str(u + 1): [(float(data.posts[p].timestamp), str(data.posts[p].id))
                                             for p in row] for u, row in enumerate(data.home)},
        # ... social-graph-redis: "<id>:followers" and "<id>:followees" likewise.
    }


# -- helpers --

def pareto2(r: Rand, scale: float, shape: float = 2.0) -> int:
    """Lomax(shape, scale) rounded down: heavy-tailed counts."""
    return int(scale * ((1 - r.random()) ** (-1 / shape) - 1))


def shuffle(r: Rand, items: list[int]) -> None:
    for i in range(len(items) - 1, 0, -1):
        j = r.below(i + 1)
        items[i], items[j] = items[j], items[i]


def token(r: Rand, n: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(alphabet[r.below(len(alphabet))] for _ in range(n))
