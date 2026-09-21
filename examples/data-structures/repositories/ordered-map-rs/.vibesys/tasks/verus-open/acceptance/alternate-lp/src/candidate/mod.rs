use vstd::atomic::*;
use vstd::prelude::*;
use vstd::resource::ghost_var::GhostVarAuth;
use vstd::resource::Loc;
use vstd::rwlock::{RwLock, RwLockPredicate};

use crate::api::{
    GetAU, LenAU, MapOperations, MaxAU, MinAU, OrderedMap, PredecessorAU, PutAU, RangeAU,
    RemoveAU, SuccessorAU,
};
use crate::contract::{
    has_key, inserted_at, is_lower_bound, predecessor_at, range_snapshot, successor_at,
    sorted_keys, the_lower_bound, unique_keys, MapConstruction, MapToken,
};

verus! {

pub(crate) struct State {
    pub(crate) entries: Vec<(u64, u64)>,
    pub(crate) auth: Tracked<GhostVarAuth<Seq<(u64, u64)>>>,
}

pub(crate) struct MapPredicate {
    pub(crate) token_id: Loc,
}

impl RwLockPredicate<State> for MapPredicate {
    closed spec fn inv(self, state: State) -> bool {
        &&& state.auth@.id() == self.token_id
        &&& state.auth@@ == state.entries@
        &&& unique_keys(state.entries@)
        &&& sorted_keys(state.entries@)
    }
}

pub(crate) struct Map {
    pub(crate) state: RwLock<State, MapPredicate>,
}

proof fn lemma_sorted_strict(entries: Seq<(u64, u64)>, a: int, b: int)
    requires
        sorted_keys(entries),
        0 <= a < b < entries.len(),
    ensures
        entries[a].0 < entries[b].0,
    decreases b - a,
{
    reveal(sorted_keys);
    if b == a + 1 {
        assert(entries[a].0 < entries[a + 1].0);
    } else {
        lemma_sorted_strict(entries, a, b - 1);
        assert(entries[b - 1].0 < entries[b].0);
    }
}

proof fn lemma_update_unique(entries: Seq<(u64, u64)>, i: int, key: u64, value: u64)
    requires
        unique_keys(entries),
        0 <= i < entries.len(),
        entries[i].0 == key,
    ensures
        unique_keys(entries.update(i, (key, value))),
{
    reveal(unique_keys);
    assert forall|j: int| 0 <= j < entries.len() implies entries.update(i, (key, value))[j].0
        == entries[j].0 by {
        if j == i {
            assert(entries.update(i, (key, value))[i].0 == key);
        } else {
            assert(entries.update(i, (key, value))[j] == entries[j]);
        }
    };
}

proof fn lemma_update_sorted(entries: Seq<(u64, u64)>, i: int, key: u64, value: u64)
    requires
        sorted_keys(entries),
        0 <= i < entries.len(),
        entries[i].0 == key,
    ensures
        sorted_keys(entries.update(i, (key, value))),
{
    reveal(sorted_keys);
    assert forall|j: int| 0 <= j < entries.len() implies entries.update(i, (key, value))[j].0
        == entries[j].0 by {
        if j == i {
            assert(entries.update(i, (key, value))[i].0 == key);
        } else {
            assert(entries.update(i, (key, value))[j] == entries[j]);
        }
    };
}

proof fn lemma_remove_unique(entries: Seq<(u64, u64)>, i: int)
    requires
        unique_keys(entries),
        0 <= i < entries.len(),
    ensures
        unique_keys(entries.remove(i)),
{
    reveal(unique_keys);
    assert forall|a: int, b: int|
        0 <= a < b < entries.remove(i).len() implies entries.remove(i)[a].0
            != entries.remove(i)[b].0 by {
        let ia = if a < i {
            a
        } else {
            a + 1
        };
        let ib = if b < i {
            b
        } else {
            b + 1
        };
        assert(0 <= ia < ib < entries.len());
        assert(entries.remove(i)[a] == entries[ia]);
        assert(entries.remove(i)[b] == entries[ib]);
    };
}

proof fn lemma_remove_sorted(entries: Seq<(u64, u64)>, i: int)
    requires
        sorted_keys(entries),
        0 <= i < entries.len(),
    ensures
        sorted_keys(entries.remove(i)),
{
    reveal(sorted_keys);
    assert forall|a: int| #![trigger entries.remove(i)[a].0] 0 <= a
        < entries.remove(i).len() - 1 implies entries.remove(i)[a].0
        < entries.remove(i)[a + 1].0 by {
        if a + 1 < i {
            assert(entries.remove(i)[a] == entries[a]);
            assert(entries.remove(i)[a + 1] == entries[a + 1]);
        } else if a + 1 == i {
            assert(entries.remove(i)[a] == entries[a]);
            assert(entries.remove(i)[a + 1] == entries[i + 1]);
            lemma_sorted_strict(entries, a, i + 1);
        } else {
            assert(entries.remove(i)[a] == entries[a + 1]);
            assert(entries.remove(i)[a + 1] == entries[a + 2]);
        }
    };
}

proof fn lemma_inserted_index(
    entries: Seq<(u64, u64)>,
    index: int,
    key: u64,
    value: u64,
    k: int,
)
    requires
        0 <= index <= entries.len(),
        0 <= k < entries.len() + 1,
    ensures
        inserted_at(entries, index, key, value)[k] == if k < index {
            entries[k]
        } else if k == index {
            (key, value)
        } else {
            entries[k - 1]
        },
{
    let left = entries.subrange(0, index).push((key, value));
    let result = inserted_at(entries, index, key, value);
    assert(result == left + entries.subrange(index, entries.len() as int));
    if k < index {
        assert(left[k] == entries.subrange(0, index)[k]);
        assert(entries.subrange(0, index)[k] == entries[k]);
        assert(result[k] == left[k]);
    } else if k == index {
        assert(left[index] == (key, value));
        assert(result[k] == left[k]);
    } else {
        assert(k >= left.len());
        assert(result[k] == entries.subrange(index, entries.len() as int)[k - left.len()]);
        assert(k - left.len() == k - index - 1);
        assert(entries.subrange(index, entries.len() as int)[k - index - 1] == entries[k - 1]);
    }
}

proof fn lemma_insert_sorted(entries: Seq<(u64, u64)>, index: int, key: u64, value: u64)
    requires
        sorted_keys(entries),
        unique_keys(entries),
        0 <= index <= entries.len(),
        forall|j: int| #![trigger entries[j].0] 0 <= j < index ==> entries[j].0 < key,
        forall|j: int| #![trigger entries[j].0] index <= j < entries.len() ==> entries[j].0 > key,
    ensures
        sorted_keys(inserted_at(entries, index, key, value)),
        unique_keys(inserted_at(entries, index, key, value)),
{
    reveal(sorted_keys);
    reveal(unique_keys);
    let result = inserted_at(entries, index, key, value);
    assert forall|i: int| #![trigger result[i].0] 0 <= i < result.len() - 1 implies result[i].0
        < result[i + 1].0 by {
        lemma_inserted_index(entries, index, key, value, i);
        lemma_inserted_index(entries, index, key, value, i + 1);
        if i + 1 < index {
            assert(result[i] == entries[i]);
            assert(result[i + 1] == entries[i + 1]);
        } else if i + 1 == index {
            assert(result[i] == entries[i]);
            assert(result[i + 1] == (key, value));
        } else if i == index {
            assert(result[i] == (key, value));
            assert(result[i + 1] == entries[index]);
        } else {
            assert(result[i] == entries[i - 1]);
            assert(result[i + 1] == entries[i]);
        }
    };
    assert forall|a: int, b: int| #![trigger result[a].0, result[b].0] 0 <= a < b < result.len()
        implies result[a].0 != result[b].0 by {
        lemma_inserted_index(entries, index, key, value, a);
        lemma_inserted_index(entries, index, key, value, b);
        if b < index {
            assert(result[a] == entries[a]);
            assert(result[b] == entries[b]);
        } else if a == index {
            assert(result[a] == (key, value));
            assert(result[b] == entries[b - 1]);
            assert(entries[b - 1].0 > key);
        } else if b == index {
            assert(result[b] == (key, value));
            assert(result[a] == entries[a]);
            assert(entries[a].0 < key);
        } else if a < index && b > index {
            assert(result[a] == entries[a]);
            assert(result[b] == entries[b - 1]);
            assert(entries[a].0 < key);
            assert(entries[b - 1].0 > key);
        } else {
            assert(result[a] == entries[a - 1]);
            assert(result[b] == entries[b - 1]);
        }
    };
}

proof fn lemma_lower_bound_unique(entries: Seq<(u64, u64)>, key: u64, i: int, j: int)
    requires
        is_lower_bound(entries, key, i),
        is_lower_bound(entries, key, j),
    ensures
        i == j,
{
    if i < j {
        assert(entries[i].0 >= key);
        assert(entries[i].0 < key);
    } else if j < i {
        assert(entries[j].0 >= key);
        assert(entries[j].0 < key);
    }
}

proof fn lemma_the_lower_bound(entries: Seq<(u64, u64)>, key: u64, i: int)
    requires
        is_lower_bound(entries, key, i),
    ensures
        the_lower_bound(entries, key) == i,
{
    assert(exists|k: int| is_lower_bound(entries, key, k));
    assert(is_lower_bound(entries, key, the_lower_bound(entries, key)));
    lemma_lower_bound_unique(entries, key, i, the_lower_bound(entries, key));
}

proof fn lemma_lower_bound_mono(
    entries: Seq<(u64, u64)>,
    start: u64,
    end: u64,
    lo: int,
    hi: int,
)
    requires
        start <= end,
        is_lower_bound(entries, start, lo),
        is_lower_bound(entries, end, hi),
    ensures
        lo <= hi,
{
    if lo > hi {
        if hi < entries.len() {
            assert(entries[hi].0 >= end);
            assert(entries[hi].0 < start);
        } else {
            assert(hi == entries.len());
            assert(lo > entries.len());
        }
    }
}

proof fn lemma_subrange_push<T>(s: Seq<T>, lo: int, i: int)
    requires
        0 <= lo <= i < s.len(),
    ensures
        s.subrange(lo, i).push(s[i]) == s.subrange(lo, i + 1),
{
    assert(s.subrange(lo, i + 1) =~= s.subrange(lo, i).push(s[i]));
}

proof fn lemma_suffix_ge(entries: Seq<(u64, u64)>, index: int, key: u64)
    requires
        sorted_keys(entries),
        0 <= index < entries.len(),
        entries[index].0 >= key,
    ensures
        forall|j: int| #![trigger entries[j].0] index <= j < entries.len() ==> entries[j].0 >= key,
{
    assert forall|j: int| #![trigger entries[j].0] index <= j < entries.len() implies entries[j].0
        >= key by {
        if j > index {
            lemma_sorted_strict(entries, index, j);
        }
    };
}

proof fn lemma_lower_bound_is(entries: Seq<(u64, u64)>, key: u64, index: int)
    requires
        sorted_keys(entries),
        0 <= index <= entries.len(),
        forall|j: int| #![trigger entries[j].0] 0 <= j < index ==> entries[j].0 < key,
        index == entries.len() || entries[index].0 >= key,
    ensures
        is_lower_bound(entries, key, index),
{
    if index < entries.len() {
        lemma_suffix_ge(entries, index, key);
    }
}

pub(crate) fn lower_bound(entries: &Vec<(u64, u64)>, key: u64) -> (result: usize)
    requires
        sorted_keys(entries@),
    ensures
        result <= entries.len(),
        forall|j: int| #![trigger entries@[j].0] 0 <= j < result ==> entries@[j].0 < key,
        result == entries.len() || entries@[result as int].0 >= key,
{
    let mut i: usize = 0;
    while i < entries.len()
        invariant
            i <= entries.len(),
            forall|j: int| #![trigger entries@[j].0] 0 <= j < i ==> entries@[j].0 < key,
        decreases entries.len() - i,
    {
        if entries[i].0 >= key {
            return i;
        }
        i = i + 1;
    }
    i
}

pub(crate) fn find_key(entries: &Vec<(u64, u64)>, key: u64) -> (result: Option<usize>)
    requires
        unique_keys(entries@),
        sorted_keys(entries@),
    ensures
        match result {
            Some(i) => i < entries.len() && entries@[i as int].0 == key,
            None => !has_key(entries@, key),
        },
{
    let mut i: usize = 0;
    while i < entries.len()
        invariant
            unique_keys(entries@),
            sorted_keys(entries@),
            i <= entries.len(),
            forall|j: int| #![trigger entries@[j].0] 0 <= j < i ==> entries@[j].0 != key,
        decreases entries.len() - i,
    {
        if entries[i].0 == key {
            return Some(i);
        }
        i = i + 1;
    }
    None
}

impl MapConstruction for Map {
    open spec fn wf(&self) -> bool {
        self.state.pred().token_id == self.token_id()
    }

    open spec fn token_id(&self) -> Loc {
        self.state.pred().token_id
    }

    fn create() -> (out: (Self, Tracked<MapToken>)) {
        let tracked (auth, token) = GhostVarAuth::new(Seq::<(u64, u64)>::empty());
        let ghost token_id = token.id();
        let ghost pred = MapPredicate { token_id };
        let state = State { entries: Vec::new(), auth: Tracked(auth) };
        proof {
            reveal(unique_keys);
            reveal(sorted_keys);
        }
        let map = Map { state: RwLock::new(state, Ghost(pred)) };
        (map, Tracked(MapToken { state: token }))
    }
}

impl MapOperations for Map {
fn put_op(
    map: &OrderedMap, key: u64, value: u64, Tracked(au): Tracked<PutAU>,
) -> (result: Option<u64>) {
    proof { map.expose_model(); }
    let (mut state, handle) = map.inner.state.acquire_write();
    match find_key(&state.entries, key) {
        Some(i) => {
            let old_value = state.entries[i].1;
            proof {
                lemma_update_unique(state.entries@, i as int, key, value);
                lemma_update_sorted(state.entries@, i as int, key, value);
                try_open_atomic_update!(au, mut token => {
                    state.auth.borrow().agree(&token.state);
                    state.auth.borrow_mut().update(
                        &mut token.state,
                        state.entries@.update(i as int, (key, value)),
                    );
                    Tracked(Commit((token, Ghost(Some(old_value)))))
                });
            }
            state.entries.set(i, (key, value));
            handle.release_write(state);
            Some(old_value)
        },
        None => {
            let index = lower_bound(&state.entries, key);
            let ghost before = state.entries@;
            proof {
                lemma_lower_bound_is(before, key, index as int);
                assert forall|j: int| #![trigger before[j].0] index <= j < before.len() implies
                    before[j].0 > key by {
                    assert(before[j].0 >= key);
                    assert(before[j].0 != key);
                };
                lemma_insert_sorted(before, index as int, key, value);
                try_open_atomic_update!(au, mut token => {
                    state.auth.borrow().agree(&token.state);
                    state.auth.borrow_mut().update(
                        &mut token.state,
                        inserted_at(before, index as int, key, value),
                    );
                    Tracked(Commit((token, Ghost(None))))
                });
            }
            state.entries.insert(index, (key, value));
            proof {
                assert(state.entries@ =~= inserted_at(before, index as int, key, value));
            }
            handle.release_write(state);
            None
        },
    }
}

fn get_op(map: &OrderedMap, key: u64, Tracked(au): Tracked<GetAU>) -> (result: Option<u64>) {
    proof { map.expose_model(); }
    let handle = map.inner.state.acquire_read();
    let state = handle.borrow();
    let result = match find_key(&state.entries, key) {
        Some(i) => Some(state.entries[i].1),
        None => None,
    };
    proof {
        try_open_atomic_update!(au, token => {
            state.auth.borrow().agree(&token.state);
            Tracked(Commit((token, Ghost(result))))
        });
    }
    handle.release_read();
    result
}

fn remove_op(
    map: &OrderedMap, key: u64, Tracked(au): Tracked<RemoveAU>,
) -> (result: Option<u64>) {
    proof { map.expose_model(); }
    let (mut state, handle) = map.inner.state.acquire_write();
    match find_key(&state.entries, key) {
        Some(i) => {
            let old_value = state.entries[i].1;
            proof {
                lemma_remove_unique(state.entries@, i as int);
                lemma_remove_sorted(state.entries@, i as int);
                try_open_atomic_update!(au, mut token => {
                    state.auth.borrow().agree(&token.state);
                    state.auth.borrow_mut().update(
                        &mut token.state,
                        state.entries@.remove(i as int),
                    );
                    Tracked(Commit((token, Ghost(Some(old_value)))))
                });
            }
            state.entries.remove(i);
            handle.release_write(state);
            Some(old_value)
        },
        None => {
            proof {
                try_open_atomic_update!(au, token => {
                    state.auth.borrow().agree(&token.state);
                    Tracked(Commit((token, Ghost(None))))
                });
            }
            handle.release_write(state);
            None
        },
    }
}

fn len_op(map: &OrderedMap, Tracked(au): Tracked<LenAU>) -> (result: usize) {
    proof { map.expose_model(); }
    let handle = map.inner.state.acquire_read();
    let state = handle.borrow();
    let result = state.entries.len();
    proof {
        try_open_atomic_update!(au, token => {
            state.auth.borrow().agree(&token.state);
            Tracked(Commit((token, Ghost(result))))
        });
    }
    handle.release_read();
    result
}

fn min_op(map: &OrderedMap, Tracked(au): Tracked<MinAU>) -> (result: Option<(u64, u64)>) {
    proof { map.expose_model(); }
    let handle = map.inner.state.acquire_read();
    let state = handle.borrow();
    let result = if state.entries.len() == 0 {
        None
    } else {
        Some(state.entries[0])
    };
    proof {
        try_open_atomic_update!(au, token => {
            state.auth.borrow().agree(&token.state);
            Tracked(Commit((token, Ghost(result))))
        });
    }
    handle.release_read();
    result
}

fn max_op(map: &OrderedMap, Tracked(au): Tracked<MaxAU>) -> (result: Option<(u64, u64)>) {
    proof { map.expose_model(); }
    let handle = map.inner.state.acquire_read();
    let state = handle.borrow();
    let result = if state.entries.len() == 0 {
        None
    } else {
        Some(state.entries[state.entries.len() - 1])
    };
    proof {
        try_open_atomic_update!(au, token => {
            state.auth.borrow().agree(&token.state);
            Tracked(Commit((token, Ghost(result))))
        });
    }
    handle.release_read();
    result
}

fn predecessor_op(
    map: &OrderedMap, key: u64, Tracked(au): Tracked<PredecessorAU>,
) -> (result: Option<(u64, u64)>) {
    proof { map.expose_model(); }
    let handle = map.inner.state.acquire_read();
    let state = handle.borrow();
    let index = lower_bound(&state.entries, key);
    let result = if index == 0 {
        None
    } else {
        Some(state.entries[index - 1])
    };
    proof {
        lemma_lower_bound_is(state.entries@, key, index as int);
        lemma_the_lower_bound(state.entries@, key, index as int);
        assert(result == predecessor_at(state.entries@, key));
        try_open_atomic_update!(au, token => {
            state.auth.borrow().agree(&token.state);
            Tracked(Commit((token, Ghost(result))))
        });
    }
    handle.release_read();
    result
}

fn successor_op(
    map: &OrderedMap, key: u64, Tracked(au): Tracked<SuccessorAU>,
) -> (result: Option<(u64, u64)>) {
    proof { map.expose_model(); }
    let handle = map.inner.state.acquire_read();
    let state = handle.borrow();
    let found = lower_bound(&state.entries, key);
    let mut index = found;
    if index < state.entries.len() && state.entries[index].0 == key {
        index = index + 1;
    }
    let result = if index < state.entries.len() {
        Some(state.entries[index])
    } else {
        None
    };
    proof {
        lemma_lower_bound_is(state.entries@, key, found as int);
        lemma_the_lower_bound(state.entries@, key, found as int);
        assert(result == successor_at(state.entries@, key));
        try_open_atomic_update!(au, token => {
            state.auth.borrow().agree(&token.state);
            Tracked(Commit((token, Ghost(result))))
        });
    }
    handle.release_read();
    result
}

fn range_op(
    map: &OrderedMap,
    start: u64,
    end: u64,
    max_items: usize,
    Tracked(au): Tracked<RangeAU>,
) -> (result: (Vec<(u64, u64)>, bool)) {
    proof { map.expose_model(); }
    let handle = map.inner.state.acquire_read();
    let state = handle.borrow();
    let lo = lower_bound(&state.entries, start);
    let hi = lower_bound(&state.entries, end);
    let mut items: Vec<(u64, u64)> = Vec::new();
    let remaining;
    if start >= end {
        remaining = false;
    } else {
        proof {
            lemma_lower_bound_is(state.entries@, start, lo as int);
            lemma_lower_bound_is(state.entries@, end, hi as int);
            lemma_lower_bound_mono(state.entries@, start, end, lo as int, hi as int);
            assert(state.entries@.subrange(lo as int, lo as int) =~= Seq::<(u64, u64)>::empty());
        }
        remaining = if max_items == 0 {
            hi > lo
        } else {
            hi - lo > max_items
        };
        let take = if max_items == 0 {
            0usize
        } else if hi - lo < max_items {
            hi - lo
        } else {
            max_items
        };
        let mut i = lo;
        while i < lo + take
            invariant
                start < end,
                lo <= hi,
                hi <= state.entries.len(),
                take <= hi - lo,
                lo <= i <= lo + take,
                items@ == state.entries@.subrange(lo as int, i as int),
                sorted_keys(state.entries@),
            decreases (lo + take) - i,
        {
            proof {
                lemma_subrange_push(state.entries@, lo as int, i as int);
            }
            items.push(state.entries[i]);
            i = i + 1;
        }
    }
    proof {
        lemma_lower_bound_is(state.entries@, start, lo as int);
        lemma_lower_bound_is(state.entries@, end, hi as int);
        lemma_the_lower_bound(state.entries@, start, lo as int);
        lemma_the_lower_bound(state.entries@, end, hi as int);
        let ghost snap = range_snapshot(
            state.entries@,
            start,
            end,
            max_items as int,
            the_lower_bound(state.entries@, start),
            the_lower_bound(state.entries@, end),
        );
        assert(snap.0 =~= items@);
        assert(snap.1 == remaining);
        try_open_atomic_update!(au, token => {
            state.auth.borrow().agree(&token.state);
            Tracked(Commit((token, Ghost((items@, remaining)))))
        });
    }
    handle.release_read();
    (items, remaining)
}
}

} // verus!
