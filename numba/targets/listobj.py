"""
Support for native homogenous lists.
"""

from __future__ import print_function, absolute_import, division

import math

from llvmlite import ir
from numba import types, cgutils, typing
from numba.targets.imputils import (builtin, builtin_attr, implement,
                                    impl_attribute, impl_attribute_generic,
                                    iternext_impl, struct_factory,
                                    impl_ret_borrowed, impl_ret_new_ref,
                                    impl_ret_untracked)
from numba.utils import cached_property


def make_list_cls(list_type):
    """
    Return the Structure representation of the given *list_type*
    (an instance of types.List).
    """
    return cgutils.create_struct_proxy(list_type)


def make_payload_cls(list_type):
    """
    Return the Structure representation of the given *list_type*'s payload
    (an instance of types.List).
    """
    return cgutils.create_struct_proxy(types.ListPayload(list_type))


def get_list_payload(context, builder, list_type, value):
    """
    Given a list value and type, get its payload structure (as a
    reference, so that mutations are seen by all).
    """
    payload_type = context.get_data_type(types.ListPayload(list_type))
    payload = context.nrt_meminfo_data(builder, value.meminfo)
    payload = builder.bitcast(payload, payload_type.as_pointer())
    return make_payload_cls(list_type)(context, builder, ref=payload)


def get_itemsize(context, list_type):
    """
    Return the item size for the given list type.
    """
    llty = context.get_data_type(list_type.dtype)
    return context.get_abi_sizeof(llty)


class _ListPayloadMixin(object):

    @property
    def size(self):
        return self._payload.size

    @size.setter
    def size(self, value):
        self._payload.size = value

    @property
    def data(self):
        return self._payload._get_ptr_by_name('data')

    def _gep(self, idx):
        return cgutils.gep(self._builder, self.data, idx)

    # Note about NRT: lists of NRT-managed objects (included nested lists)
    # cannot be handled right now, as the number of nested meminfos is
    # dynamic.

    def getitem(self, idx):
        ptr = self._gep(idx)
        return self._builder.load(ptr)

    def setitem(self, idx, val):
        ptr = self._gep(idx)
        self._builder.store(val, ptr)

    def inititem(self, idx, val):
        ptr = self._gep(idx)
        self._builder.store(val, ptr)
    
    def fix_index(self, idx):
        """
        Fix negative indices by adding the size to them.  Positive
        indices are left untouched.
        """
        is_negative = self._builder.icmp_signed('<', idx,
                                                ir.Constant(idx.type, 0))
        wrapped_index = self._builder.add(idx, self.size)
        return self._builder.select(is_negative, wrapped_index, idx)

    def is_out_of_bounds(self, idx):
        """
        Return whether the index is out of bounds.
        """
        underflow = self._builder.icmp_signed('<', idx,
                                              ir.Constant(idx.type, 0))
        overflow = self._builder.icmp_signed('>=', idx, self.size)
        return self._builder.or_(underflow, overflow)

    def clamp_index(self, idx):
        """
        Clamp the index in [0, size].
        """
        builder = self._builder
        idxptr = cgutils.alloca_once_value(builder, idx)

        zero = ir.Constant(idx.type, 0)
        size = self.size

        underflow = self._builder.icmp_signed('<', idx, zero)
        with builder.if_then(underflow, likely=False):
            builder.store(zero, idxptr)
        overflow = self._builder.icmp_signed('>=', idx, size)
        with builder.if_then(overflow, likely=False):
            builder.store(size, idxptr)
            
        return builder.load(idxptr)

    def guard_index(self, idx, msg):
        """
        Raise an error if the index is out of bounds.
        """
        with self._builder.if_then(self.is_out_of_bounds(idx), likely=False):
            self._context.call_conv.return_user_exc(self._builder,
                                                    IndexError, (msg,))

    def fix_slice(self, slice):
        """
        Fix slice start and stop to be valid (inclusive and exclusive, resp)
        indexing bounds.
        """
        # See PySlice_GetIndicesEx()
        builder = self._builder
        size = self.size
        zero = ir.Constant(size.type, 0)
        minus_one = ir.Constant(size.type, -1)

        def fix_bound(bound_name, lower_repl, upper_repl):
            bound = getattr(slice, bound_name)
            bound = self.fix_index(bound)
            # Store value
            setattr(slice, bound_name, bound)
            # Still negative? => clamp to lower_repl
            underflow = builder.icmp_signed('<', bound, zero)
            with builder.if_then(underflow, likely=False):
                setattr(slice, bound_name, lower_repl)
            # Greater than size? => clamp to upper_repl
            overflow = builder.icmp_signed('>', bound, size)
            with builder.if_then(overflow, likely=False):
                setattr(slice, bound_name, upper_repl)
        
        with builder.if_else(cgutils.is_neg_int(builder, slice.step)) as (if_neg_step, if_pos_step):
            with if_pos_step:
                # < 0 => 0; >= size => size
                fix_bound('start', zero, size)
                fix_bound('stop', zero, size)
            with if_neg_step:
                # < 0 => -1; >= size => size - 1
                lower = minus_one
                upper = builder.add(size, minus_one)
                fix_bound('start', lower, upper)
                fix_bound('stop', lower, upper)


class ListInstance(_ListPayloadMixin):
    
    def __init__(self, context, builder, list_type, list_val):
        self._context = context
        self._builder = builder
        self._ty = list_type
        self._list = make_list_cls(list_type)(context, builder, list_val)
        self._itemsize = get_itemsize(context, list_type)

    @property
    def _payload(self):
        # This cannot be cached as it can be reallocated
        return get_list_payload(self._context, self._builder, self._ty, self._list)

    @property
    def value(self):
        return self._list._getvalue()

    @property
    def meminfo(self):
        return self._list.meminfo

    @classmethod
    def allocate(cls, context, builder, list_type, nitems):
        intp_t = context.get_value_type(types.intp)

        if isinstance(nitems, int):
            nitems = ir.Constant(intp_t, nitems)

        payload_type = context.get_data_type(types.ListPayload(list_type))
        payload_size = context.get_abi_sizeof(payload_type)

        itemsize = get_itemsize(context, list_type)
        
        # Total allocation size = <payload header size> + nitems * itemsize
        allocsize, ovf = cgutils.muladd_with_overflow(builder, nitems,
                                                      ir.Constant(intp_t, itemsize),
                                                      ir.Constant(intp_t, payload_size))
        with builder.if_then(ovf, likely=False):
            context.call_conv.return_user_exc(builder, MemoryError,
                                              ("cannot allocate list",))

        meminfo = context.nrt_meminfo_varsize_alloc(builder, size=allocsize)
        cgutils.guard_memory_error(context, builder, meminfo,
                                   "cannot allocate list")

        self = cls(context, builder, list_type, None)
        self._list.meminfo = meminfo
        self._payload.allocated = nitems
        self._payload.size = ir.Constant(intp_t, 0)  # for safety
        return self

    def resize(self, new_size):
        """
        Ensure the list is properly sized for the new size.
        """
        def _payload_realloc(new_allocated):
            payload_type = context.get_data_type(types.ListPayload(self._ty))
            payload_size = context.get_abi_sizeof(payload_type)

            allocsize, ovf = cgutils.muladd_with_overflow(
                builder, new_allocated,
                ir.Constant(intp_t, itemsize),
                ir.Constant(intp_t, payload_size))
            with builder.if_then(ovf, likely=False):
                context.call_conv.return_user_exc(builder, MemoryError,
                                                  ("cannot resize list",))

            ptr = context.nrt_meminfo_varsize_realloc(builder, self._list.meminfo,
                                                      size=allocsize)
            cgutils.guard_memory_error(context, builder, ptr,
                                       "cannot resize list")
            self._payload.allocated = new_allocated

        context = self._context
        builder = self._builder
        intp_t = new_size.type

        itemsize = get_itemsize(context, self._ty)
        allocated = self._payload.allocated

        one = ir.Constant(intp_t, 1)
        two = ir.Constant(intp_t, 2)
        eight = ir.Constant(intp_t, 8)

        # allocated < new_size
        is_too_small = builder.icmp_signed('<', allocated, new_size)
        # (allocated >> 2) > new_size
        is_too_large = builder.icmp_signed('>', builder.ashr(allocated, two), new_size)

        with builder.if_then(is_too_large, likely=False):
            # Exact downsize to requested size
            # NOTE: is_too_large must be aggressive enough to avoid repeated
            # upsizes and downsizes when growing a list.
            _payload_realloc(new_size)

        with builder.if_then(is_too_small, likely=False):
            # Upsize with moderate over-allocation (size + size >> 2 + 8)
            new_allocated = builder.add(eight,
                                        builder.add(new_size,
                                                    builder.ashr(new_size, two)))
            _payload_realloc(new_allocated)

        self._payload.size = new_size

    def move(self, dest_idx, src_idx, count):
        """
        Move `count` elements from `src_idx` to `dest_idx`.
        """
        dest_ptr = self._gep(dest_idx)
        src_ptr = self._gep(src_idx)
        cgutils.memmove(self._builder, dest_ptr, src_ptr,
                        count, itemsize=self._itemsize)


class ListIterInstance(_ListPayloadMixin):
    
    def __init__(self, context, builder, iter_type, iter_val):
        self._context = context
        self._builder = builder
        self._ty = iter_type
        self._iter = make_listiter_cls(iter_type)(context, builder, iter_val)

    @classmethod
    def from_list(cls, context, builder, iter_type, list_val):
        list_inst = ListInstance(context, builder, iter_type.list_type, list_val)
        self = cls(context, builder, iter_type, None)
        index = context.get_constant(types.intp, 0)
        self._iter.index = cgutils.alloca_once_value(builder, index)
        self._iter.meminfo = list_inst.meminfo
        return self

    @property
    def _payload(self):
        # This cannot be cached as it can be reallocated
        return get_list_payload(self._context, self._builder,
                                self._ty.list_type, self._iter)

    @property
    def value(self):
        return self._iter._getvalue()

    @property
    def index(self):
        return self._builder.load(self._iter.index)

    @index.setter
    def index(self, value):
        self._builder.store(value, self._iter.index)


#-------------------------------------------------------------------------------
# Constructors

def build_list(context, builder, list_type, items):
    """
    Build a list of the given type, containing the given items.
    """
    nitems = len(items)
    inst = ListInstance.allocate(context, builder, list_type, nitems)
    # Populate list
    inst.size = context.get_constant(types.intp, nitems)
    for i, val in enumerate(items):
        inst.setitem(context.get_constant(types.intp, i), val)

    return impl_ret_new_ref(context, builder, list_type, inst.value)


@builtin
@implement(list, types.Kind(types.IterableType))
def list_constructor(context, builder, sig, args):

    def list_impl(iterable):
        res = []
        res.extend(iterable)
        return res

    return context.compile_internal(builder, list_impl, sig, args)


#-------------------------------------------------------------------------------
# Various operations

@builtin
@implement(types.len_type, types.Kind(types.List))
def list_len(context, builder, sig, args):
    inst = ListInstance(context, builder, sig.args[0], args[0])
    return inst.size


@struct_factory(types.ListIter)
def make_listiter_cls(iterator_type):
    """
    Return the Structure representation of the given *iterator_type* (an
    instance of types.ListIter).
    """
    return cgutils.create_struct_proxy(iterator_type)

@builtin
@implement('getiter', types.Kind(types.List))
def getiter_list(context, builder, sig, args):
    inst = ListIterInstance.from_list(context, builder, sig.return_type, args[0])
    return impl_ret_borrowed(context, builder, sig.return_type, inst.value)

@builtin
@implement('iternext', types.Kind(types.ListIter))
@iternext_impl
def iternext_listiter(context, builder, sig, args, result):
    inst = ListIterInstance(context, builder, sig.args[0], args[0])

    index = inst.index
    nitems = inst.size
    is_valid = builder.icmp_signed('<', index, nitems)
    result.set_valid(is_valid)

    with builder.if_then(is_valid):
        result.yield_(inst.getitem(index))
        inst.index = builder.add(index, context.get_constant(types.intp, 1))


@builtin
@implement('getitem', types.Kind(types.List), types.Kind(types.Integer))
def getitem_list(context, builder, sig, args):
    inst = ListInstance(context, builder, sig.args[0], args[0])
    index = args[1]

    index = inst.fix_index(index)
    result = inst.getitem(index)

    return impl_ret_borrowed(context, builder, sig.return_type, result)

@builtin
@implement('setitem', types.Kind(types.List), types.Kind(types.Integer), types.Any)
def setitem_list(context, builder, sig, args):
    inst = ListInstance(context, builder, sig.args[0], args[0])
    index = args[1]
    value = args[2]

    index = inst.fix_index(index)
    inst.setitem(index, value)
    return context.get_dummy_value()


@builtin
@implement('getitem', types.Kind(types.List), types.slice3_type)
def getslice_list(context, builder, sig, args):
    from .builtins import Slice

    inst = ListInstance(context, builder, sig.args[0], args[0])
    slice = Slice(context, builder, value=args[1])
    cgutils.guard_invalid_slice(context, builder, slice)
    inst.fix_slice(slice)

    # Allocate result and populate it
    result_size = cgutils.get_slice_length(builder, slice)
    result = ListInstance.allocate(context, builder, sig.return_type,
                                   result_size)
    result.size = result_size
    with cgutils.for_range_slice_generic(builder, slice.start, slice.stop,
                                         slice.step) as (pos_range, neg_range):
        with pos_range as (idx, count):
            value = inst.getitem(idx)
            result.inititem(count, value)
        with neg_range as (idx, count):
            value = inst.getitem(idx)
            result.inititem(count, value)

    return impl_ret_new_ref(context, builder, sig.return_type, result.value)

@builtin
@implement('setitem', types.Kind(types.List), types.slice3_type, types.Any)
def setitem_list(context, builder, sig, args):
    from .builtins import Slice

    dest = ListInstance(context, builder, sig.args[0], args[0])
    slice = Slice(context, builder, value=args[1])
    src = ListInstance(context, builder, sig.args[2], args[2])

    cgutils.guard_invalid_slice(context, builder, slice)
    dest.fix_slice(slice)

    src_size = src.size
    avail_size = cgutils.get_slice_length(builder, slice)
    size_delta = builder.sub(src.size, avail_size)

    zero = ir.Constant(size_delta.type, 0)
    one = ir.Constant(size_delta.type, 1)

    with builder.if_else(builder.icmp_signed('==', slice.step, one)) as (then, otherwise):
        with then:
            # Slice step == 1 => we can resize

            # Compute the real stop, e.g. for dest[2:0] = [...]
            real_stop = builder.add(slice.start, avail_size)
            # Size of the list tail, after the end of slice
            tail_size = builder.sub(dest.size, real_stop)

            with builder.if_then(builder.icmp_signed('>', size_delta, zero)):
                # Grow list then move list tail
                dest.resize(builder.add(dest.size, size_delta))
                dest.move(builder.add(real_stop, size_delta), real_stop,
                          tail_size)

            with builder.if_then(builder.icmp_signed('<', size_delta, zero)):
                # Move list tail then shrink list
                dest.move(builder.add(real_stop, size_delta), real_stop,
                          tail_size)
                dest.resize(builder.add(dest.size, size_delta))

            dest_offset = slice.start

            with cgutils.for_range(builder, src_size) as src_index:
                value = src.getitem(src_index)
                dest.setitem(builder.add(src_index, dest_offset), value)
        
        with otherwise:
            with builder.if_then(builder.icmp_signed('!=', size_delta, zero)):
                msg = "cannot resize extended list slice with step != 1"
                context.call_conv.return_user_exc(builder, ValueError, (msg,))

            with cgutils.for_range_slice_generic(
                builder, slice.start, slice.stop, slice.step) as (pos_range, neg_range):
                with pos_range as (index, count):
                    value = src.getitem(count)
                    dest.setitem(index, value)
                with neg_range as (index, count):
                    value = src.getitem(count)
                    dest.setitem(index, value)

    return context.get_dummy_value()


@builtin
@implement('delitem', types.Kind(types.List), types.slice3_type)
def setitem_list(context, builder, sig, args):
    from .builtins import Slice

    inst = ListInstance(context, builder, sig.args[0], args[0])
    slice = Slice(context, builder, value=args[1])

    cgutils.guard_invalid_slice(context, builder, slice)
    inst.fix_slice(slice)

    slice_len = cgutils.get_slice_length(builder, slice)

    zero = ir.Constant(slice_len.type, 0)
    one = ir.Constant(slice_len.type, 1)
    
    with builder.if_then(builder.icmp_signed('!=', slice.step, one), likely=False):
        msg = "unsupported del list[start:stop:step] with step != 1"
        context.call_conv.return_user_exc(builder, NotImplementedError, (msg,))

    # Compute the real stop, e.g. for dest[2:0]
    start = slice.start
    real_stop = builder.add(start, slice_len)
    # Size of the list tail, after the end of slice
    tail_size = builder.sub(inst.size, real_stop)
    inst.move(start, real_stop, tail_size)
    inst.resize(builder.sub(inst.size, slice_len))

    return context.get_dummy_value()


@builtin
@implement("in", types.Any, types.Kind(types.List))
def in_list(context, builder, sig, args):
    def list_contains_impl(value, lst):
        for elem in lst:
            if elem == value:
                return True
        return False

    return context.compile_internal(builder, list_contains_impl, sig, args)


# XXX should there be a specific module for Sequence or collection base classes?
@builtin
@implement(bool, types.Kind(types.Sequence))
def sequence_bool(context, builder, sig, args):
    def sequence_bool_impl(seq):
        return len(seq) != 0

    return context.compile_internal(builder, sequence_bool_impl, sig, args)


@builtin
@implement("+", types.Kind(types.List), types.Kind(types.List))
def list_add(context, builder, sig, args):
    a = ListInstance(context, builder, sig.args[0], args[0])
    b = ListInstance(context, builder, sig.args[1], args[1])

    a_size = a.size
    b_size = b.size
    nitems = builder.add(a_size, b_size)
    dest = ListInstance.allocate(context, builder, sig.return_type, nitems)
    dest.size = nitems

    with cgutils.for_range(builder, a_size) as src_index:
        value = a.getitem(src_index)
        dest.setitem(src_index, value)
    with cgutils.for_range(builder, b_size) as src_index:
        value = b.getitem(src_index)
        dest.setitem(builder.add(src_index, a_size), value)

    return impl_ret_new_ref(context, builder, sig.return_type, dest.value)

@builtin
@implement("+=", types.Kind(types.List), types.Kind(types.List))
def list_add_inplace(context, builder, sig, args):
    assert sig.args[0].dtype == sig.args[1].dtype
    dest = _list_extend_list(context, builder, sig, args)

    return impl_ret_borrowed(context, builder, sig.return_type, dest.value)


@builtin
@implement("*", types.Kind(types.List), types.Kind(types.Integer))
def list_mul(context, builder, sig, args):
    src = ListInstance(context, builder, sig.args[0], args[0])
    src_size = src.size

    mult = args[1]
    zero = ir.Constant(mult.type, 0)
    mult = builder.select(cgutils.is_neg_int(builder, mult), zero, mult)
    nitems = builder.mul(mult, src_size)

    dest = ListInstance.allocate(context, builder, sig.return_type, nitems)
    dest.size = nitems

    with cgutils.for_range_slice(builder, zero, nitems, src_size, inc=True) as (dest_offset, _):
        with cgutils.for_range(builder, src_size) as src_index:
            value = src.getitem(src_index)
            dest.setitem(builder.add(src_index, dest_offset), value)

    return impl_ret_new_ref(context, builder, sig.return_type, dest.value)

@builtin
@implement("*=", types.Kind(types.List), types.Kind(types.Integer))
def list_mul_inplace(context, builder, sig, args):
    inst = ListInstance(context, builder, sig.args[0], args[0])
    src_size = inst.size

    mult = args[1]
    zero = ir.Constant(mult.type, 0)
    mult = builder.select(cgutils.is_neg_int(builder, mult), zero, mult)
    nitems = builder.mul(mult, src_size)

    inst.resize(nitems)

    with cgutils.for_range_slice(builder, src_size, nitems, src_size, inc=True) as (dest_offset, _):
        with cgutils.for_range(builder, src_size) as src_index:
            value = inst.getitem(src_index)
            inst.setitem(builder.add(src_index, dest_offset), value)

    return impl_ret_borrowed(context, builder, sig.return_type, inst.value)


#-------------------------------------------------------------------------------
# Methods

@builtin
@implement("list.append", types.Kind(types.List), types.Any)
def list_append(context, builder, sig, args):
    inst = ListInstance(context, builder, sig.args[0], args[0])
    item = args[1]

    n = inst.size
    new_size = builder.add(n, ir.Constant(n.type, 1))
    inst.resize(new_size)
    inst.setitem(n, item)

    return context.get_dummy_value()

@builtin
@implement("list.clear", types.Kind(types.List))
def list_clear(context, builder, sig, args):
    inst = ListInstance(context, builder, sig.args[0], args[0])
    inst.resize(context.get_constant(types.intp, 0))

    return context.get_dummy_value()

@builtin
@implement("list.copy", types.Kind(types.List))
def list_copy(context, builder, sig, args):
    def list_copy_impl(lst):
        return list(lst)

    return context.compile_internal(builder, list_copy_impl, sig, args)

@builtin
@implement("list.count", types.Kind(types.List), types.Any)
def list_count(context, builder, sig, args):

    def list_count_impl(lst, value):
        res = 0
        for elem in lst:
            if elem == value:
                res += 1
        return res

    return context.compile_internal(builder, list_count_impl, sig, args)

def _list_extend_list(context, builder, sig, args):
    src = ListInstance(context, builder, sig.args[1], args[1])
    dest = ListInstance(context, builder, sig.args[0], args[0])

    src_size = src.size
    dest_size = dest.size
    nitems = builder.add(src_size, dest_size)
    dest.resize(nitems)
    dest.size = nitems

    with cgutils.for_range(builder, src_size) as src_index:
        value = src.getitem(src_index)
        dest.setitem(builder.add(src_index, dest_size), value)

    return dest

@builtin
@implement("list.extend", types.Kind(types.List), types.Kind(types.IterableType))
def list_extend(context, builder, sig, args):
    if isinstance(sig.args[1], types.List) and sig.args[0].dtype == sig.args[1].dtype:
        # Specialize for same-type list operands, for speed
        _list_extend_list(context, builder, sig, args)
        return context.get_dummy_value()

    def list_extend(lst, iterable):
        # Speed hack to avoid NRT refcount operations inside the loop
        meth = lst.append
        for v in iterable:
            meth(v)

    return context.compile_internal(builder, list_extend, sig, args)

@builtin
@implement("list.index", types.Kind(types.List), types.Any)
def list_index(context, builder, sig, args):

    def list_index_impl(lst, value):
        for i in range(len(lst)):
            if lst[i] == value:
                return i
        # XXX references are leaked when raising
        raise ValueError("value not in list")

    return context.compile_internal(builder, list_index_impl, sig, args)

@builtin
@implement("list.index", types.Kind(types.List), types.Any,
           types.Kind(types.Integer))
def list_index(context, builder, sig, args):

    def list_index_impl(lst, value, start):
        n = len(lst)
        if start < 0:
            start += n
            if start < 0:
                start = 0
        for i in range(start, len(lst)):
            if lst[i] == value:
                return i
        # XXX references are leaked when raising
        raise ValueError("value not in list")

    return context.compile_internal(builder, list_index_impl, sig, args)

@builtin
@implement("list.index", types.Kind(types.List), types.Any,
           types.Kind(types.Integer), types.Kind(types.Integer))
def list_index(context, builder, sig, args):

    def list_index_impl(lst, value, start, stop):
        n = len(lst)
        if start < 0:
            start += n
            if start < 0:
                start = 0
        if stop < 0:
            stop += n
        if stop > n:
            stop = n
        for i in range(start, stop):
            if lst[i] == value:
                return i
        # XXX references are leaked when raising
        raise ValueError("value not in list")

    return context.compile_internal(builder, list_index_impl, sig, args)

@builtin
@implement("list.insert", types.Kind(types.List), types.Kind(types.Integer),
           types.Any)
def list_insert(context, builder, sig, args):
    inst = ListInstance(context, builder, sig.args[0], args[0])
    index = inst.fix_index(args[1])
    index = inst.clamp_index(index)
    value = args[2]

    n = inst.size
    one = ir.Constant(n.type, 1)
    new_size = builder.add(n, one)
    inst.resize(new_size)
    inst.move(builder.add(index, one), index, builder.sub(n, index))
    inst.setitem(index, value)

    return context.get_dummy_value()

@builtin
@implement("list.pop", types.Kind(types.List))
def list_pop(context, builder, sig, args):
    inst = ListInstance(context, builder, sig.args[0], args[0])

    n = inst.size
    cgutils.guard_zero(context, builder, n,
                       (IndexError, "pop from empty list"))
    n = builder.sub(n, ir.Constant(n.type, 1))
    res = inst.getitem(n)
    inst.resize(n)
    return res

@builtin
@implement("list.pop", types.Kind(types.List), types.Kind(types.Integer))
def list_pop(context, builder, sig, args):
    inst = ListInstance(context, builder, sig.args[0], args[0])
    idx = inst.fix_index(args[1])

    n = inst.size
    cgutils.guard_zero(context, builder, n,
                       (IndexError, "pop from empty list"))
    inst.guard_index(idx, "pop index out of range")

    res = inst.getitem(idx)

    one = ir.Constant(n.type, 1)
    n = builder.sub(n, ir.Constant(n.type, 1))
    inst.move(idx, builder.add(idx, one), builder.sub(n, idx))
    inst.resize(n)
    return res

@builtin
@implement("list.remove", types.Kind(types.List), types.Any)
def list_remove(context, builder, sig, args):

    def list_remove_impl(lst, value):
        for i in range(len(lst)):
            if lst[i] == value:
                lst.pop(i)
                return
        # XXX references are leaked when raising
        raise ValueError("list.remove(x): x not in list")

    return context.compile_internal(builder, list_remove_impl, sig, args)

@builtin
@implement("list.reverse", types.Kind(types.List))
def list_reverse(context, builder, sig, args):

    def list_reverse_impl(lst):
        for a in range(0, len(lst) // 2):
            b = -a - 1
            lst[a], lst[b] = lst[b], lst[a]

    return context.compile_internal(builder, list_reverse_impl, sig, args)
