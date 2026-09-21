use vstd::atomic::{AtomicUpdate, Commit};
use vstd::invariant::*;
use vstd::open_atomic_invariant_in_proof;
use vstd::iset::ISet;
use vstd::prelude::*;

use crate::candidate::Stack;
use crate::contract::{LifoToken, StackConstruction};

verus! {

pub(crate) struct ClientTokenInv<T>(core::marker::PhantomData<T>);

impl<T> InvariantPredicate<(vstd::resource::Loc, usize), LifoToken<T>> for ClientTokenInv<T> {
    open spec fn inv(constant: (vstd::resource::Loc, usize), token: LifoToken<T>) -> bool {
        &&& token.id() == constant.0
        &&& token.contents().len() <= constant.1
    }
}

pub open spec const LIFO_CLIENT_INV: int = 734_202;

/// Public stack handle. Its tracked invariant is erased and owns no runtime
/// synchronization. All executable synchronization remains in `inner`.
pub struct MpmcStack<T> {
    pub(crate) inner: Stack<T>,
    pub(crate) token: Tracked<AtomicInvariant<(vstd::resource::Loc, usize), LifoToken<T>, ClientTokenInv<T>>>,
}

impl<T> MpmcStack<T> {
    pub closed spec fn wf(&self) -> bool {
        &&& self.inner.wf()
        &&& self.token@.constant() == (self.inner.token_id(), self.inner.capacity())
        &&& self.token@.namespace() == LIFO_CLIENT_INV
    }

    pub closed spec fn token_id(&self) -> vstd::resource::Loc {
        self.inner.token_id()
    }

    pub closed spec fn capacity(&self) -> usize {
        self.inner.capacity()
    }

    pub(crate) proof fn expose_model(&self)
        requires
            self.wf(),
        ensures
            self.inner.wf(),
            self.token_id() == self.inner.token_id(),
            self.capacity() == self.inner.capacity(),
    {
        assert(self.token_id() == self.inner.token_id());
        assert(self.capacity() == self.inner.capacity());
    }

}

pub(crate) type PushCommit<T> = Commit<(LifoToken<T>, Ghost<Result<(), T>>)>;
pub(crate) type PopCommit<T> = Commit<(LifoToken<T>, Ghost<Option<T>>)>;
pub(crate) type LenCommit<T> = Commit<(LifoToken<T>, Ghost<usize>)>;

pub type PushAU<T> = AtomicUpdate<LifoToken<T>, PushCommit<T>, PushPred<T>>;
pub type PopAU<T> = AtomicUpdate<LifoToken<T>, PopCommit<T>, PopPred<T>>;
pub type LenAU<T> = AtomicUpdate<LifoToken<T>, LenCommit<T>, LenPred<T>>;

/// Fixed bridge from logical atomicity to candidate code. Implementations may
/// choose where to resolve or transfer the AU, but cannot weaken this contract.
pub(crate) trait StackOperations<T> {
    fn push_op(
        stack: &MpmcStack<T>,
        value: T,
        au: Tracked<PushAU<T>>,
    ) -> (result: Result<(), T>)
        requires
            stack.wf(),
            au@.pred().args(stack, value),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn pop_op(stack: &MpmcStack<T>, au: Tracked<PopAU<T>>) -> (result: Option<T>)
        requires
            stack.wf(),
            au@.pred().args(stack),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;

    fn len_op(stack: &MpmcStack<T>, au: Tracked<LenAU<T>>) -> (result: usize)
        requires
            stack.wf(),
            au@.pred().args(stack),
        ensures
            au@.resolves(),
            result == au@.output()@.1@;
}

impl<T> MpmcStack<T> {
pub fn new(capacity: usize) -> (out: Self)
    requires
        capacity > 0,
    ensures
        out.wf(),
        out.capacity() == capacity,
{
    let (inner, Tracked(token)) = Stack::create(capacity);
    let ghost constant = (token.id(), capacity);
    let tracked token = AtomicInvariant::new(constant, token, LIFO_CLIENT_INV);
    MpmcStack { inner, token: Tracked(token) }
}

pub fn push(&self, value: T) -> (result: Result<(), T>)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    push_atomic(self, value) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: PushCommit<T> = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}

pub fn pop(&self) -> (result: Option<T>)
    requires self.wf(),
{
    let Tracked(credit) = vstd::invariant::create_open_invariant_credit();
    pop_atomic(self) atomically |update| {
        open_atomic_invariant!(credit => self.token.borrow() => token => {
            let tracked updated: PopCommit<T> = update(token);
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
            let tracked updated: LenCommit<T> = update(token);
            let tracked (next, Ghost(_result)) = updated.get();
            token = next;
        });
    }
}

pub fn is_empty(&self) -> (result: bool)
    requires self.wf(),
{
    self.len() == 0
}
}

/// Logically atomic bounded LIFO push.
pub(crate) fn push_atomic<T>(stack: &MpmcStack<T>, value: T) -> (result: Result<(), T>)
    atomically (atomic_update) {
        type PushPred,
        (old_token: LifoToken<T>) -> (commit: PushCommit<T>),
        requires
            old_token.id() == stack.token_id(),
            old_token.contents().len() <= stack.capacity(),
        ensures
            commit@.0.id() == old_token.id(),
            match commit@.1@ {
                Ok(()) => {
                    &&& old_token.contents().len() < stack.capacity()
                    &&& commit@.0.contents() == old_token.contents().push(value)
                },
                Err(returned) => {
                    &&& returned == value
                    &&& old_token.contents().len() == stack.capacity()
                    &&& commit@.0.contents() == old_token.contents()
                },
            },
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires stack.wf(),
    ensures result == commit@.1@,
{
    <Stack<T> as StackOperations<T>>::push_op(stack, value, Tracked(atomic_update))
}

/// Logically atomic strict LIFO pop. The returned value is the last element.
pub(crate) fn pop_atomic<T>(stack: &MpmcStack<T>) -> (result: Option<T>)
    atomically (atomic_update) {
        type PopPred,
        (old_token: LifoToken<T>) -> (commit: PopCommit<T>),
        requires
            old_token.id() == stack.token_id(),
            old_token.contents().len() <= stack.capacity(),
        ensures
            commit@.0.id() == old_token.id(),
            match commit@.1@ {
                Some(value) => {
                    &&& old_token.contents().len() > 0
                    &&& value == old_token.contents()[old_token.contents().len() - 1]
                    &&& commit@.0.contents() == old_token.contents().drop_last()
                },
                None => {
                    &&& old_token.contents().len() == 0
                    &&& commit@.0.contents() == old_token.contents()
                },
            },
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires stack.wf(),
    ensures result == commit@.1@,
{
    <Stack<T> as StackOperations<T>>::pop_op(stack, Tracked(atomic_update))
}

/// Logically atomic size observation.
pub(crate) fn len_atomic<T>(stack: &MpmcStack<T>) -> (result: usize)
    atomically (atomic_update) {
        type LenPred,
        (old_token: LifoToken<T>) -> (commit: LenCommit<T>),
        requires
            old_token.id() == stack.token_id(),
            old_token.contents().len() <= stack.capacity(),
        ensures
            commit@.0 == old_token,
            commit@.1@ == old_token.contents().len(),
        outer_mask ISet::<int>::full(),
        inner_mask none,
    },
    requires stack.wf(),
    ensures result == commit@.1@,
{
    <Stack<T> as StackOperations<T>>::len_op(stack, Tracked(atomic_update))
}

} // verus!
