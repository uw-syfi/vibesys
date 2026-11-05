"""social-network-k8s: DeathStarBench Social Network on Kubernetes (spec example).

Written against the intended vibesys-evaluator API (api/public-api.txt); it
does not run yet. It replaces the task's run.py (benchmark path) and the Go
benchmark adapter (evaluator/internal/apps/socialnetwork, internal/timeline,
cmd/loadgen, cmd/seed). Mix 500/400/100/5/5, the distributions, both phases,
the 0.7x open-loop rate and the validity rules are those of workload/*.toml.

    python3 /task/bench.py benchmark --project-root ROOT --vs-output PATH
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

import dataset as ds
import vibesys_evaluator as vse
from vibesys_evaluator import Call, Rand, Versions
from vibesys_evaluator.deploy import k8s
from vibesys_evaluator.service import Operation, Request, Workload, get, post

PAGE = 10
BOUND_SECS = 1.0  # INTERFACE.md: home timelines and follow changes, 1 s after acknowledgement

# ---------------------------------------------------------------- (2) seed


def seed(data: ds.Dataset) -> None:
    """Write the dataset straight into the data tier (in-cluster Job, before the services).

    The API would assign post ids, request ids and timestamps itself and take
    far too long; the documents are the ones the services write. Caches
    (memcached, user-timeline-redis) stay empty.
    """
    import pymongo  # in the task image
    import redis

    for service, documents in ds.mongo_documents(data).items():
        database, collection = ds.MONGO[service]
        target = pymongo.MongoClient(f"mongodb://{service}:27017")[database][collection]
        target.insert_many(documents, ordered=False)
        for key, unique in ds.INDEXES.get(service, []):  # run.py's create_indexes
            target.create_index(key, unique=unique)
    for service, keys in ds.redis_entries(data).items():
        pipe = redis.Redis(host=service, port=6379).pipeline(transaction=False)
        for key, entries in keys.items():
            pipe.zadd(key, {member: score for score, member in entries})
        pipe.execute()


# -------------------------------------------------- (1) system under test

STATELESS = ["compose-post-service", "home-timeline-service", "media-service", "nginx-thrift",
             "post-storage-service", "social-graph-service", "text-service", "unique-id-service",
             "url-shorten-service", "user-mention-service", "user-service", "user-timeline-service"]
PROBE_USER = "1999999999"  # far from the dataset's ids: probes leave its caches alone

SYSTEM = k8s.App(
    name="social-network-k8s",
    manifest="k8s/manifest.yaml",
    images={"SOCIAL_NETWORK_IMAGE": "socialNetwork"},  # C++ services and nginx-thrift with its Lua
    data_tier="tier=data",
    seed=seed,
    gateway="http://nginx-thrift:8080",
    # Two replicas of every service: readiness waits for every replica, and
    # each probe must answer twice per nginx-thrift replica on fresh connections.
    ready=[
        get("/"),
        get("/wrk2-api/user-timeline/read", query={"user_id": PROBE_USER, "start": "0", "stop": "1"}),
        get("/wrk2-api/home-timeline/read", query={"user_id": PROBE_USER, "start": "0", "stop": "1"}),
    ],
    # The checker's restart hook: `bench.py ctl restart DEPLOYMENT` deletes one
    # pod and waits for all replicas and the probes; `ctl flush CACHE` empties a
    # cache in place. home-timeline-redis is a store, not a cache: never flushed.
    restartable=STATELESS,
    caches={"post-storage-memcached": "memcached", "url-shorten-memcached": "memcached",
            "user-memcached": "memcached", "media-memcached": "memcached",
            "social-graph-redis": "redis", "user-timeline-redis": "redis"},
)


# --------------------------------------------------------------- (3) model

@dataclass
class LivePost:
    """A post this sample composed, as sent; `served` is its identity when first seen."""
    marker: str
    author: int
    text: str
    mentions: tuple[int, ...]
    url: str
    media: tuple[tuple[str, str], ...]
    call: Call | None = None  # the compose, once applied
    served: dict[str, Any] | None = None


@dataclass
class Model:
    """The seeded dataset plus this sample's composes and follow changes.

    Cross-phase state: every phase pod builds a fresh Model and the library
    replays the sample's history into it (every write of the earlier phases,
    with its outcome and times) through `apply`. So the open phases validate
    exactly against what the closed phases wrote. This replaces the adapter's
    write journal, `--state-out`/`--state-in`, and run.py's ConfigMaps.
    """

    data: ds.Dataset
    rand: Rand
    next_seq: int = 0
    posts: dict[str, LivePost] = field(default_factory=dict)  # by marker
    by_author: dict[int, list[LivePost]] = field(default_factory=dict)
    by_mention: dict[int, list[LivePost]] = field(default_factory=dict)
    # A compose is in its author's user timeline once acknowledged (bound 0),
    # and in home timelines 1 s after.
    in_user_timeline: Versions[str, bool] = field(default_factory=lambda: Versions(bound_secs=0.0))
    in_home: Versions[str, bool] = field(default_factory=lambda: Versions(bound_secs=BOUND_SECS))
    edges: Versions[tuple[int, int], bool] = field(default_factory=lambda: Versions(bound_secs=BOUND_SECS))
    changed: dict[int, set[int]] = field(default_factory=dict)  # follower -> authors
    prefix: str = ""  # this sample's marker namespace

    def __post_init__(self) -> None:
        self.prefix = f"m{self.rand.child('markers').below(1 << 40):010x}"

    def follows(self, u: int, v: int) -> bool:
        """The latest state sent (pending writes included): what generation builds on."""
        return self.edges.latest((u, v), self.data.follows(u, v))


def new_post(m: Model, author: int, seq: int, rand: Rand) -> LivePost:
    """A compose: 2-25 words, a unique #marker, a followee mention 20%, a URL 15%, media 5%."""
    words = [ds.WORDS[rand.below(len(ds.WORDS))] for _ in range(min(2 + ds.pareto2(rand, 5), 25))]
    mentions: tuple[int, ...] = ()
    row = m.data.followees[author]
    if row and rand.random() < 0.2:
        mentions = (row[rand.below(len(row))],)
        words.insert(rand.below(len(words) + 1), f"@{m.data.usernames[mentions[0]]}")
    marker = f"{m.prefix}{seq:x}"
    words.append(f"#{marker}")
    url = f"http://example{rand.below(10**6)}.com" if rand.random() < 0.15 else ""
    media = tuple((str(10**16 + rand.below(9 * 10**16)), "png")
                  for _ in range(1 + rand.below(2))) if rand.random() < 0.05 else ()
    return LivePost(marker, author, " ".join(words + ([url] if url else [])), mentions, url, media)


# --------------------------------------------------- (3) request distributions

def page(rand: Rand) -> int:
    """First page 80%, second 12%, third 5%, pages 4-7 3%."""
    u = rand.random()
    return 0 if u < 0.80 else PAGE if u < 0.92 else 2 * PAGE if u < 0.97 else PAGE * (3 + rand.below(4))


def popular(m: Model, rand: Rand) -> int:  # Zipf 1 over follower count
    return m.data.popularity_rank[rand.zipf(ds.USERS, 1.0)]


def active(m: Model, rand: Rand) -> int:  # Zipf 0.75 over post count
    return m.data.activity_rank[rand.zipf(ds.USERS, 0.75)]


def timeline(kind: str, user: int, start: int):
    return get(f"/wrk2-api/{kind}/read",
               query={"user_id": str(user + 1), "start": str(start), "stop": str(start + PAGE)})


# ---------------------------------------------------------- (3) operations

def user_timeline_read(m: Model, rand: Rand) -> Request:
    user, start = popular(m, rand), page(rand)
    return Request(timeline("user-timeline", user, start), info={"user": user, "start": start})


def home_timeline_read(m: Model, rand: Rand) -> Request:
    user, start = active(m, rand), page(rand)
    return Request(timeline("home-timeline", user, start), info={"user": user, "start": start})


def compose_post(m: Model, rand: Rand) -> Request:
    """Compose, then read the author's user timeline: a two-step template.

    The second step runs after the first answered, on the same connection
    slot; it must hold the post (read your write, no delay).
    """
    author, seq, seed = active(m, rand), m.next_seq, rand.below(1 << 62)
    p = new_post(m, author, seq, Rand(seed))
    form = {"username": m.data.usernames[author], "user_id": str(author + 1), "text": p.text,
            "media_ids": "[" + ",".join(f'"{i}"' for i, _ in p.media) + "]",
            "media_types": "[" + ",".join(f'"{t}"' for _, t in p.media) + "]", "post_type": "0"}
    return Request(
        post("/wrk2-api/post/compose", form=form),
        timeline("user-timeline", author, 0),
        info={"author": author, "seq": seq, "seed": seed},
    )


def change_edge(follow: bool):
    """Follow a popular user not followed yet, or drop a followee (active users)."""
    def build(m: Model, rand: Rand) -> Request:
        for attempt in range(64):
            r = rand.child("attempt", attempt)
            u = active(m, r)
            if follow:
                v = popular(m, r)
            elif m.data.followees[u]:
                v = m.data.followees[u][r.below(len(m.data.followees[u]))]
            else:
                continue
            if v != u and m.follows(u, v) != follow:
                break
        else:
            raise ValueError("no edge to change in 64 draws")
        path = "/wrk2-api/user/follow" if follow else "/wrk2-api/user/unfollow"
        # One change per edge in flight: the loadgen holds the edge's key.
        return Request(post(path, form={"user_id": str(u + 1), "followee_id": str(v + 1)}),
                       info={"u": u, "v": v, "on": follow}, exclusive=f"edge:{u}:{v}")
    return build


# ------------------------------------------------- (3) apply: writes -> model

def apply_compose(m: Model, call: Call) -> None:
    author, seq = call.info["author"], call.info["seq"]
    p = new_post(m, author, seq, Rand(call.info["seed"]))
    p.call = call
    m.next_seq = max(m.next_seq, seq + 1)
    m.posts[p.marker] = p
    m.by_author.setdefault(author, []).append(p)
    for v in p.mentions:
        m.by_mention.setdefault(v, []).append(p)
    m.in_user_timeline.write(p.marker, True, call)
    m.in_home.write(p.marker, True, call)


def apply_edge(m: Model, call: Call) -> None:
    u, v = call.info["u"], call.info["v"]
    m.edges.write((u, v), call.info["on"], call)
    m.changed.setdefault(u, set()).add(v)


# ------------------------------------------------------ (3) check: responses

@dataclass(frozen=True)
class Candidate:
    """A post allowed in a timeline: seeded or live, required or optional."""
    timestamp: int  # 0 for a live post not seen yet (newer than every seeded post)
    key: str  # seeded post id, or a live post's marker
    must: bool


def certain(m: Model, edge: tuple[int, int], start: float, end: float) -> bool | None:
    """The edge's state over [start, end] if the bound makes it certain, else None."""
    allowed = m.edges.allowed(edge, m.data.follows(*edge), start=start, end=end)
    return allowed[0] if len(allowed) == 1 else None


def live(m: Model, p: LivePost, versions: Versions[str, bool], call: Call) -> Candidate | None:
    assert call.start is not None and call.end is not None
    allowed = versions.allowed(p.marker, False, start=call.start, end=call.end)
    if True not in allowed:
        return None
    stamp = int(p.served["timestamp"]) if p.served else 0
    return Candidate(stamp, p.marker, allowed == (True,))


def user_candidates(m: Model, user: int, call: Call) -> list[Candidate]:
    out = [Candidate(m.data.posts[i].timestamp, str(m.data.posts[i].id), True)
           for i in m.data.user_posts[user]]
    return out + [c for p in m.by_author.get(user, []) if (c := live(m, p, m.in_user_timeline, call))]


def home_candidates(m: Model, user: int, call: Call) -> list[Candidate]:
    """INTERFACE.md rules 3 and 4: followers and mentioned users, edges by their history."""
    assert call.start is not None and call.end is not None
    changed = m.changed.get(user, set())
    out = [Candidate(m.data.posts[i].timestamp, str(m.data.posts[i].id), True)
           for i in m.data.home[user]
           if m.data.posts[i].author not in changed or user in m.data.posts[i].mentions]
    authors = {a for a in m.data.followees[user] if a not in changed}
    lives = {p.marker: p for p in m.by_mention.get(user, [])}
    lives |= {p.marker: p for a in authors for p in m.by_author.get(a, [])}
    out += [c for p in lives.values() if (c := live(m, p, m.in_home, call))]
    for author in changed:  # push (at compose time) and pull (at read time) must agree
        pull = certain(m, (user, author), call.start, call.start)
        for i in m.data.user_posts[author]:
            p = m.data.posts[i]
            if user in p.mentions:
                continue
            push = certain(m, (user, author), p.timestamp / 1000, p.timestamp / 1000)
            if push is not False or pull is not False:
                out.append(Candidate(p.timestamp, str(p.id), push is True and pull is True))
        for lp in m.by_author.get(author, []):
            if lp.marker in lives or lp.call is None or lp.call.start is None or lp.call.end is None:
                continue
            push = certain(m, (user, author), lp.call.start, lp.call.end)
            c = live(m, lp, m.in_home, call)
            if c and (push is not False or pull is not False):
                out.append(Candidate(c.timestamp, c.key, c.must and push is True and pull is True))
    return out


def check_page(m: Model, call: Call, step: int, candidates: list[Candidate], start: int,
               must_hold: str = "") -> str | None:
    """The page is timeline[start:start+PAGE] of some timeline with every required
    candidate, any optional ones and nothing else, newest first, no duplicates;
    every post has its stored (seeded) or sent (live) content."""
    served = call.responses[step].json()
    by_key = {c.key: c for c in candidates}
    keys = []
    for item in served:
        marker = next((w[1:] for w in item["text"].split() if w.startswith("#m")), "")
        key = marker if marker in m.posts else str(item["post_id"])
        if key not in by_key:
            return f"post {item['post_id']} must not be in this timeline"
        if problem := check_content(m, key, item):
            return problem
        keys.append(key)
    if len(set(keys)) != len(keys):
        return "duplicate posts"
    stamps = [int(item["timestamp"]) for item in served]
    if stamps != sorted(stamps, reverse=True):
        return "not newest first"
    if must_hold and must_hold not in keys:
        return f"the author's timeline lacks the post just composed ({must_hold})"
    if served:  # how many timeline posts precede the page: at least the required newer ones
        newer_must = sum(1 for c in candidates if c.must and c.key not in keys
                         and (c.timestamp == 0 or c.timestamp > stamps[0]))
        newer_any = sum(1 for c in candidates if c.key not in keys
                        and (c.timestamp == 0 or c.timestamp > stamps[0]))
        if not newer_must <= start <= newer_any:
            return f"page at {start} after {newer_must}..{newer_any} newer posts"
    elif sum(1 for c in candidates if c.must) > start:
        return f"empty page at {start}, but {sum(1 for c in candidates if c.must)} posts are required"
    return None


def check_content(m: Model, key: str, item: dict[str, Any]) -> str | None:
    """Seeded: as stored. Live: as sent, timestamp within the compose; learned on first sight."""
    p = m.posts.get(key)
    if p is None:
        seeded = m.data.posts[int(key) - ds.FIRST_POST_ID]
        return None if item["text"] == seeded.text else f"post {key}: text {item['text']!r}"
    if p.served is None:  # check_* run in start order, so the first sight comes first
        p.served = dict(item)
        assert p.call is not None and p.call.start is not None and p.call.end is not None
        if not p.call.start * 1000 - 1 <= int(item["timestamp"]) <= p.call.end * 1000 + 1:
            return f"post {p.marker}: timestamp outside its compose"
    elif {k: p.served[k] for k in ("post_id", "timestamp")} != {k: item[k] for k in ("post_id", "timestamp")}:
        return f"post {p.marker} changed identity"
    return None


def check_user_timeline(m: Model, call: Call) -> str | None:
    user, start = call.info["user"], call.info["start"]
    return check_page(m, call, 0, user_candidates(m, user, call), start)


def check_home_timeline(m: Model, call: Call) -> str | None:
    user, start = call.info["user"], call.info["start"]
    return check_page(m, call, 0, home_candidates(m, user, call), start)


def check_compose(m: Model, call: Call) -> str | None:
    if call.responses[0].status != 200:
        return f"compose: HTTP {call.responses[0].status}"
    author = call.info["author"]
    marker = f"{m.prefix}{call.info['seq']:x}"
    return check_page(m, call, 1, user_candidates(m, author, call), 0, must_hold=marker)


def check_edge(m: Model, call: Call) -> str | None:
    return None if call.responses[0].status == 200 else f"HTTP {call.responses[0].status}"


# ------------------------------------------------------------- (3) workload

WORKLOAD = Workload(
    model=Model,
    operations=[
        Operation(name="user_timeline_read", weight=500, build=user_timeline_read,
                  check=check_user_timeline),
        Operation(name="home_timeline_read", weight=400, build=home_timeline_read,
                  check=check_home_timeline),
        Operation(name="compose_post", weight=100, build=compose_post, check=check_compose,
                  apply=apply_compose),
        Operation(name="follow", weight=5, build=change_edge(True), check=check_edge, apply=apply_edge),
        Operation(name="unfollow", weight=5, build=change_edge(False), check=check_edge, apply=apply_edge),
    ],
)


# ------------------------------------------------------------ (4) benchmark

def benchmark(run: vse.Run) -> None:
    """Closed-loop capacity (2 trials), then p99 at 0.7x of it (3 open-loop trials).

    The open phases see every compose and follow change of the closed ones:
    the run's history carries them (see Model). The plan bound for the
    closed loop is lower than the default: this application is slower, and a
    smaller plan is generated faster.
    """
    closed = [run.closed_loop(clients=32, secs=20, warmup=10, max_ops_per_sec=10_000) for _ in range(2)]
    throughput = statistics.median(phase.ops_per_sec for phase in closed)
    opened = [run.open_loop(rate=0.7 * throughput, secs=60, warmup=10, arrivals="uniform")
              for _ in range(3)]
    run.report({
        "operations_per_second": throughput,
        "latency_ms.p99": statistics.median(phase.p99_ms for phase in opened),
    })


# ------------------------------------------------------------- (5) validity

if __name__ == "__main__":
    vse.main(
        dataset=ds.generate,
        system=SYSTEM,
        workload=WORKLOAD,
        benchmark=benchmark,
        metrics={
            "operations_per_second": vse.Metric(unit="ops/s", direction="max"),
            "latency_ms.p99": vse.Metric(unit="ms", direction="min"),
        },
        validity=vse.Validity(max_error_rate=0.0, min_sustained_ratio=0.95, min_ops_per_operation=1),
    )
