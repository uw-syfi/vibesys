use vstd::atomic::{AtomicUpdate, Commit};
use vstd::invariant::*;
use vstd::open_atomic_invariant_in_proof;
use vstd::iset::ISet;
use vstd::prelude::*;

use crate::candidate::Map;
use crate::contract::{
    has_key, inserted_at, is_lower_bound, predecessor_at, range_snapshot, successor_at,
    sorted_keys, the_lower_bound, unique_keys, MapConstruction, MapToken,
};

verus! {

pub(crate) struct ClientTokenInv(());

impl InvariantPredicate<vstd::resource::Loc, MapToken> for ClientTokenInv {
    open spec fn inv(constant: vstd::resource::Loc, token: MapToken) -> bool {
        &&& token.id() == constant
        &&& unique_keys(token.contents())
        &&& sorted_keys(token.contents())
    }
}

pub open spec const ORDERED_MAP_CLIENT_INV: int = 734_204;

pub struct OrderedMap {
    pub(crate) inner: Map,
    pub(crate) token: Tracked<AtomicInvariant<vstd::resource::Loc, MapToken, ClientTokenInv>>,
}

impl OrderedMap {
    pub closed spec fn wf(&self) -> bool {
        &&& self.inner.wf()
        &&& self.token@.constant() == self.inner.token_id()
        &&& self.token@.namespace() == ORDERED_MAP_CLIENT_INV
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
pub(crate) type NeighborCommit = Commit<(MapToken, Ghost<Option<(u64, u64)>>)>;
pub(crate) type RangeCommit = Commit<(MapToken, Ghost<(Seq<(u64, u64)>, bool)>)>;

pub type PutAU = AtomicUpdate<MapToken, PutCommit, PutPred>;
pub type GetAU = AtomicUpdate<MapToken, GetCommit, GetPred>;
pub type RemoveAU = AtomicUpdate<MapToken, RemoveCommit, RemovePred>;
pub type LenAU = AtomicUpdate<MapToken, LenCommit, LenPred>;
pub type MinAU = AtomicUpdate<MapToken, NeighborCommit, MinPred>;
pub type MaxAU = AtomicUpdate<MapToken, NeighborCommit, MaxPred>;
pub type PredecessorAU = AtomicUpdate<MapToken, NeighborCommit, PredecessorPred>;
pub type SuccessorAU = AtomicUpdate<MapToken, NeighborCommit, SuccessorPred>;
pub type RangeAU = AtomicUpdate<MapToken, RangeCommit, RangePred>;

pub(crate) trait MapOperations {
    fn put_op(
        map: &OrderedMap,
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

    fn get_op(map: &OrderedMap, key: u64, au: Tracked<GetAU>) -> (result: Option<u64>)
        requires
            map.wf(),
            au@.pred().args(map, key),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn remove_op(map: &OrderedMap, key: u64, au: Tracked<RemoveAU>) -> (result: Option<u64>)
        requires
            map.wf(),
            au@.pred().args(map, key),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn len_op(map: &OrderedMap, au: Tracked<LenAU>) -> (result: usize)
        requires
            map.wf(),
            au@.pred().args(map),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn min_op(map: &OrderedMap, au: Tracked<MinAU>) -> (result: Option<(u64, u64)>)
        requires
            map.wf(),
            au@.pred().args(map),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn max_op(map: &OrderedMap, au: Tracked<MaxAU>) -> (result: Option<(u64, u64)>)
        requires
            map.wf(),
            au@.pred().args(map),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn predecessor_op(
        map: &OrderedMap,
        key: u64,
        au: Tracked<PredecessorAU>,
    ) -> (result: Option<(u64, u64)>)
        requires
            map.wf(),
            au@.pred().args(map, key),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn successor_op(
        map: &OrderedMap,
        key: u64,
        au: Tracked<SuccessorAU>,
    ) -> (result: Option<(u64, u64)>)
        requires
            map.wf(),
            au@.pred().args(map, key),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn range_op(
        map: &OrderedMap,
        start: u64,
        end: u64,
        max_items: usize,
        au: Tracked<RangeAU>,
    ) -> (result: (Vec<(u64, u64)>, bool))
        requires
            map.wf(),
            au@.pred().args(map, start, end, max_items),
        ensures
            au@.resolves(),
            result.0@ == au@.output()@.1@.0,
            result.1 == au@.output()@.1@.1;
}

impl OrderedMap {
pub fn new() -> (out: Self)
    ensures
        out.wf(),
{
    let (inner, Tracked(token)) = Map::create();
    let ghost constant = token.id();
    let tracked token = AtomicInvariant::new(constant, token, ORDERED_MAP_CLIENT_INV);
    OrderedMap { inner, token: Tracked(token) }
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

pub fn min(&self) -> (result: Option<(u64, u64)>)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    min_atomic(self) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: NeighborCommit = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}

pub fn max(&self) -> (result: Option<(u64, u64)>)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    max_atomic(self) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: NeighborCommit = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}

pub fn predecessor(&self, key: u64) -> (result: Option<(u64, u64)>)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    predecessor_atomic(self, key) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: NeighborCommit = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}

pub fn successor(&self, key: u64) -> (result: Option<(u64, u64)>)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    successor_atomic(self, key) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: NeighborCommit = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}

pub fn range(&self, start: u64, end: u64, max_items: usize) -> (result: (Vec<(u64, u64)>, bool))
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    range_atomic(self, start, end, max_items) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: RangeCommit = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}
}

pub(crate) fn put_atomic(map: &OrderedMap, key: u64, value: u64) -> (result: Option<u64>)
    atomically (atomic_update) {
        type PutPred,
        (old_token: MapToken) -> (commit: PutCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
            sorted_keys(old_token.contents()),
        ensures
            commit@.0.id() == old_token.id(),
            unique_keys(commit@.0.contents()),
            sorted_keys(commit@.0.contents()),
            match commit@.1@ {
                None => {
                    &&& !has_key(old_token.contents(), key)
                    &&& exists|i: int|
                        #![trigger inserted_at(old_token.contents(), i, key, value)]
                        is_lower_bound(old_token.contents(), key, i)
                            && commit@.0.contents() == inserted_at(
                            old_token.contents(),
                            i,
                            key,
                            value,
                        )
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

pub(crate) fn get_atomic(map: &OrderedMap, key: u64) -> (result: Option<u64>)
    atomically (atomic_update) {
        type GetPred,
        (old_token: MapToken) -> (commit: GetCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
            sorted_keys(old_token.contents()),
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

pub(crate) fn remove_atomic(map: &OrderedMap, key: u64) -> (result: Option<u64>)
    atomically (atomic_update) {
        type RemovePred,
        (old_token: MapToken) -> (commit: RemoveCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
            sorted_keys(old_token.contents()),
        ensures
            commit@.0.id() == old_token.id(),
            unique_keys(commit@.0.contents()),
            sorted_keys(commit@.0.contents()),
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

pub(crate) fn len_atomic(map: &OrderedMap) -> (result: usize)
    atomically (atomic_update) {
        type LenPred,
        (old_token: MapToken) -> (commit: LenCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
            sorted_keys(old_token.contents()),
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

pub(crate) fn min_atomic(map: &OrderedMap) -> (result: Option<(u64, u64)>)
    atomically (atomic_update) {
        type MinPred,
        (old_token: MapToken) -> (commit: NeighborCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
            sorted_keys(old_token.contents()),
        ensures
            commit@.0 == old_token,
            commit@.1@ == (if old_token.contents().len() == 0 {
                None
            } else {
                Some(old_token.contents()[0])
            }),
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires map.wf(),
    ensures result == commit@.1@,
{
    <Map as MapOperations>::min_op(map, Tracked(atomic_update))
}

pub(crate) fn max_atomic(map: &OrderedMap) -> (result: Option<(u64, u64)>)
    atomically (atomic_update) {
        type MaxPred,
        (old_token: MapToken) -> (commit: NeighborCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
            sorted_keys(old_token.contents()),
        ensures
            commit@.0 == old_token,
            commit@.1@ == (if old_token.contents().len() == 0 {
                None
            } else {
                Some(old_token.contents()[old_token.contents().len() - 1])
            }),
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires map.wf(),
    ensures result == commit@.1@,
{
    <Map as MapOperations>::max_op(map, Tracked(atomic_update))
}

pub(crate) fn predecessor_atomic(map: &OrderedMap, key: u64) -> (result: Option<(u64, u64)>)
    atomically (atomic_update) {
        type PredecessorPred,
        (old_token: MapToken) -> (commit: NeighborCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
            sorted_keys(old_token.contents()),
        ensures
            commit@.0 == old_token,
            commit@.1@ == predecessor_at(old_token.contents(), key),
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires map.wf(),
    ensures result == commit@.1@,
{
    <Map as MapOperations>::predecessor_op(map, key, Tracked(atomic_update))
}

pub(crate) fn successor_atomic(map: &OrderedMap, key: u64) -> (result: Option<(u64, u64)>)
    atomically (atomic_update) {
        type SuccessorPred,
        (old_token: MapToken) -> (commit: NeighborCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
            sorted_keys(old_token.contents()),
        ensures
            commit@.0 == old_token,
            commit@.1@ == successor_at(old_token.contents(), key),
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires map.wf(),
    ensures result == commit@.1@,
{
    <Map as MapOperations>::successor_op(map, key, Tracked(atomic_update))
}

pub(crate) fn range_atomic(
    map: &OrderedMap,
    start: u64,
    end: u64,
    max_items: usize,
) -> (result: (Vec<(u64, u64)>, bool))
    atomically (atomic_update) {
        type RangePred,
        (old_token: MapToken) -> (commit: RangeCommit),
        requires
            old_token.id() == map.token_id(),
            unique_keys(old_token.contents()),
            sorted_keys(old_token.contents()),
        ensures
            commit@.0 == old_token,
            commit@.1@ == range_snapshot(
                old_token.contents(),
                start,
                end,
                max_items as int,
                the_lower_bound(old_token.contents(), start),
                the_lower_bound(old_token.contents(), end),
            ),
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires map.wf(),
    ensures
        result.0@ == commit@.1@.0,
        result.1 == commit@.1@.1,
{
    <Map as MapOperations>::range_op(map, start, end, max_items, Tracked(atomic_update))
}

} // verus!
