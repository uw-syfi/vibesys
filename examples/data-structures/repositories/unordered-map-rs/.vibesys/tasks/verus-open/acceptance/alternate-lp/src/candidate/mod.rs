use vstd::atomic::*;
use vstd::prelude::*;
use vstd::resource::ghost_var::GhostVarAuth;
use vstd::resource::Loc;
use vstd::rwlock::{RwLock, RwLockPredicate};

use crate::api::{ConcurrentMap, GetAU, LenAU, MapOperations, PutAU, RemoveAU};
use crate::contract::{has_key, unique_keys, MapConstruction, MapToken};

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
    }
}

pub(crate) struct Map {
    pub(crate) state: RwLock<State, MapPredicate>,
}

proof fn lemma_push_unique(entries: Seq<(u64, u64)>, key: u64, value: u64)
    requires
        unique_keys(entries),
        !has_key(entries, key),
    ensures
        unique_keys(entries.push((key, value))),
{
    assert forall|i: int, j: int|
        0 <= i < j < entries.push((key, value)).len() implies entries.push((key, value))[i].0
            != entries.push((key, value))[j].0 by {
        if j < entries.len() {
            assert(entries.push((key, value))[i] == entries[i]);
            assert(entries.push((key, value))[j] == entries[j]);
        } else {
            assert(j == entries.len());
            assert(entries.push((key, value))[j].0 == key);
            if i < entries.len() {
                assert(entries.push((key, value))[i] == entries[i]);
                assert(entries[i].0 != key);
            }
        }
    };
}

proof fn lemma_update_unique(entries: Seq<(u64, u64)>, i: int, key: u64, value: u64)
    requires
        unique_keys(entries),
        0 <= i < entries.len(),
        entries[i].0 == key,
    ensures
        unique_keys(entries.update(i, (key, value))),
{
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

pub(crate) fn find_key(entries: &Vec<(u64, u64)>, key: u64) -> (result: Option<usize>)
    requires
        unique_keys(entries@),
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
            i <= entries.len(),
            forall|j: int| 0 <= j < i ==> entries@[j].0 != key,
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
        let map = Map { state: RwLock::new(state, Ghost(pred)) };
        (map, Tracked(MapToken { state: token }))
    }
}

impl MapOperations for Map {
fn put_op(
    map: &ConcurrentMap, key: u64, value: u64, Tracked(au): Tracked<PutAU>,
) -> (result: Option<u64>) {
    proof { map.expose_model(); }
    let (mut state, handle) = map.inner.state.acquire_write();
    match find_key(&state.entries, key) {
        Some(i) => {
            let old_value = state.entries[i].1;
            proof {
                lemma_update_unique(state.entries@, i as int, key, value);
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
            proof {
                lemma_push_unique(state.entries@, key, value);
                try_open_atomic_update!(au, mut token => {
                    state.auth.borrow().agree(&token.state);
                    state.auth.borrow_mut().update(
                        &mut token.state,
                        state.entries@.push((key, value)),
                    );
                    Tracked(Commit((token, Ghost(None))))
                });
            }
            state.entries.push((key, value));
            handle.release_write(state);
            None
        },
    }
}

fn get_op(map: &ConcurrentMap, key: u64, Tracked(au): Tracked<GetAU>) -> (result: Option<u64>) {
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
    map: &ConcurrentMap, key: u64, Tracked(au): Tracked<RemoveAU>,
) -> (result: Option<u64>) {
    proof { map.expose_model(); }
    let (mut state, handle) = map.inner.state.acquire_write();
    match find_key(&state.entries, key) {
        Some(i) => {
            let old_value = state.entries[i].1;
            proof {
                lemma_remove_unique(state.entries@, i as int);
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

fn len_op(map: &ConcurrentMap, Tracked(au): Tracked<LenAU>) -> (result: usize) {
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
}

} // verus!
