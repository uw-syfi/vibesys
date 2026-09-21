use vstd::prelude::*;
use vstd::resource::ghost_var::GhostVar;
use vstd::resource::Loc;

verus! {

/// The client-owned half of the abstract LIFO state. The top is the last
/// sequence element.
pub tracked struct LifoToken<T> {
    pub state: GhostVar<Seq<T>>,
}

impl<T> LifoToken<T> {
    pub open spec fn id(self) -> Loc {
        self.state.id()
    }

    pub open spec fn contents(self) -> Seq<T> {
        self.state@
    }
}

/// Fixed construction contract for a candidate-owned concurrent stack.
pub(crate) trait StackConstruction<T>: Sized {
    spec fn wf(&self) -> bool;

    spec fn token_id(&self) -> Loc;

    spec fn capacity(&self) -> usize;

    fn create(capacity: usize) -> (out: (Self, Tracked<LifoToken<T>>))
        requires
            capacity > 0,
        ensures
            out.0.wf(),
            out.0.capacity() == capacity,
            out.1@.id() == out.0.token_id(),
            out.1@.contents() == Seq::<T>::empty();
}

} // verus!
