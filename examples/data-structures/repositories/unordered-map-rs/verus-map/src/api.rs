use vstd::atomic::{AtomicUpdate, Commit};
use vstd::invariant::*;
use vstd::open_atomic_invariant_in_proof;
use vstd::iset::ISet;
use vstd::prelude::*;

use crate::candidate::Map;
use crate::contract::{has_key, unique_keys, MapConstruction, MapToken};

verus! {

pub(crate) struct ClientTokenInv(());

impl InvariantPredicate<vstd::resource::Loc, MapToken> for ClientTokenInv {
    open spec fn inv(constant: vstd::resource::Loc, token: MapToken) -> bool {
        &&& token.id() == constant
        &&& unique_keys(token.contents())
    }
}

pub open spec const MAP_CLIENT_INV: int = 734_203;

pub struct ConcurrentMap {
    pub(crate) inner: Map,
    pub(crate) token: Tracked<AtomicInvariant<vstd::resource::Loc, MapToken, ClientTokenInv>>,
}

impl ConcurrentMap {
    pub closed spec fn wf(&self) -> bool {
        &&& self.inner.wf()
        &&& self.token@.constant() == self.inner.token_id()
        &&& self.token@.namespace() == MAP_CLIENT_INV
    }

    pub closed spec fn token_id(&self) -> vstd::resource::Loc {
        self.inner.token_id()
    }

    pub(crate) proof fn expose_model(&self)
        requires
            self.wf(),
        ensures
            self.inner.wf(),
            self.token_id() == self.inner.token_id(),
    {
        assert(self.token_id() == self.inner.token_id());
    }
}

pub(crate) type PutCommit = Commit<(MapToken, Ghost<Option<u64>>)>;
pub(crate) type GetCommit = Commit<(MapToken, Ghost<Option<u64>>)>;
pub(crate) type RemoveCommit = Commit<(MapToken, Ghost<Option<u64>>)>;
pub(crate) type LenCommit = Commit<(MapToken, Ghost<usize>)>;

pub type PutAU = AtomicUpdate<MapToken, PutCommit, PutPred>;
pub type GetAU = AtomicUpdate<MapToken, GetCommit, GetPred>;
pub type RemoveAU = AtomicUpdate<MapToken, RemoveCommit, RemovePred>;
pub type LenAU = AtomicUpdate<MapToken, LenCommit, LenPred>;

pub(crate) trait MapOperations {
    fn put_op(
        map: &ConcurrentMap,
        key: u64,
        value: u64,
        au: Tracked<PutAU>,
    ) -> (result: Option<u64>)
        requires
            map.wf(),
            au@.pred().args(map, key, value),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn get_op(map: &ConcurrentMap, key: u64, au: Tracked<GetAU>) -> (result: Option<u64>)
        requires
            map.wf(),
            au@.pred().args(map, key),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn remove_op(map: &ConcurrentMap, key: u64, au: Tracked<RemoveAU>) -> (result: Option<u64>)
        requires
            map.wf(),
            au@.pred().args(map, key),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn len_op(map: &ConcurrentMap, au: Tracked<LenAU>) -> (result: usize)
        requires
            map.wf(),
            au@.pred().args(map),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;
}

impl ConcurrentMap {
pub fn new() -> (out: Self)
    ensures
        out.wf(),
{
    let (inner, Tracked(token)) = Map::create();
    let ghost constant = token.id();
    let tracked token = AtomicInvariant::new(constant, token, MAP_CLIENT_INV);
    ConcurrentMap { inner, token: Tracked(token) }
}

pub fn put(&self, key: u64, value: u64) -> (result: Option<u64>)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    put_atomic(self, key, value) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: PutCommit = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}

pub fn get(&self, key: u64) -> (result: Option<u64>)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    get_atomic(self, key) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: GetCommit = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}

pub fn remove(&self, key: u64) -> (result: Option<u64>)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    remove_atomic(self, key) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: RemoveCommit = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}

pub fn len(&self) -> (result: usize)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    len_atomic(self) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: LenCommit = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}
}

pub(crate) fn put_atomic(map: &ConcurrentMap, key: u64, value: u64) -> (result: Option<u64>)
    atomically (atomic_update) {
        type PutPred,
        (old_token: MapToken) -> (commit: PutCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
        ensures
            commit@.0.id() == old_token.id(),
            unique_keys(commit@.0.contents()),
            match commit@.1@ {
                None => {
                    &&& !has_key(old_token.contents(), key)
                    &&& commit@.0.contents() == old_token.contents().push((key, value))
                },
                Some(old_value) => {
                    exists|i: int|
                        #![trigger old_token.contents()[i]]
                        0 <= i < old_token.contents().len()
                            && old_token.contents()[i].0 == key
                            && old_token.contents()[i].1 == old_value
                            && commit@.0.contents()
                                == old_token.contents().update(i, (key, value))
                },
            },
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires map.wf(),
    ensures result == commit@.1@,
{
    <Map as MapOperations>::put_op(map, key, value, Tracked(atomic_update))
}

pub(crate) fn get_atomic(map: &ConcurrentMap, key: u64) -> (result: Option<u64>)
    atomically (atomic_update) {
        type GetPred,
        (old_token: MapToken) -> (commit: GetCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
        ensures
            commit@.0 == old_token,
            match commit@.1@ {
                None => !has_key(old_token.contents(), key),
                Some(value) => exists|i: int|
                    #![trigger old_token.contents()[i]]
                    0 <= i < old_token.contents().len()
                        && old_token.contents()[i] == (key, value),
            },
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires map.wf(),
    ensures result == commit@.1@,
{
    <Map as MapOperations>::get_op(map, key, Tracked(atomic_update))
}

pub(crate) fn remove_atomic(map: &ConcurrentMap, key: u64) -> (result: Option<u64>)
    atomically (atomic_update) {
        type RemovePred,
        (old_token: MapToken) -> (commit: RemoveCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
        ensures
            commit@.0.id() == old_token.id(),
            unique_keys(commit@.0.contents()),
            match commit@.1@ {
                None => {
                    &&& !has_key(old_token.contents(), key)
                    &&& commit@.0.contents() == old_token.contents()
                },
                Some(old_value) => exists|i: int|
                    #![trigger old_token.contents()[i]]
                    0 <= i < old_token.contents().len()
                        && old_token.contents()[i] == (key, old_value)
                        && commit@.0.contents() == old_token.contents().remove(i),
            },
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires map.wf(),
    ensures result == commit@.1@,
{
    <Map as MapOperations>::remove_op(map, key, Tracked(atomic_update))
}

pub(crate) fn len_atomic(map: &ConcurrentMap) -> (result: usize)
    atomically (atomic_update) {
        type LenPred,
        (old_token: MapToken) -> (commit: LenCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
        ensures
            commit@.0 == old_token,
            commit@.1@ == old_token.contents().len(),
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires map.wf(),
    ensures result == commit@.1@,
{
    <Map as MapOperations>::len_op(map, Tracked(atomic_update))
}

} // verus!
