# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import functools
import inspect
import types
from abc import ABC, abstractmethod
from collections import defaultdict
from itertools import chain
from typing import (
    Any,
    DefaultDict,
    Dict,
    Generator,
    Generic,
    Iterable,
    Iterator,
    List,
    Tuple,
    TypeVar,
    Union,
    get_args,
)
from types import UnionType
from collections.abc import Callable
from mypy_extensions import TypedDict

from monkeytype.compat import (
    is_any,
    is_generic,
    is_generic_of,
    is_typed_dict,
    is_union,
    name_of_generic,
    types_equal,
)

DUMMY_TYPED_DICT_NAME = "DUMMY_NAME"
DUMMY_REQUIRED_TYPED_DICT_NAME = "REQUIRED_TYPED_DICT_NAME"
DUMMY_OPTIONAL_TYPED_DICT_NAME = "OPTIONAL_TYPED_DICT_NAME"


# Functions like shrink_types and get_type construct new types at runtime.
# Mypy cannot currently type these functions, so the type signatures for this
# file live in typing.pyi.


def is_list(typ: type) -> bool:
    return is_generic(typ) and name_of_generic(typ) == "List"


def make_typed_dict(*, required_fields=None, optional_fields=None) -> type:
    required_fields = required_fields or {}
    optional_fields = optional_fields or {}
    assert required_fields.keys().isdisjoint(optional_fields.keys())
    return TypedDict(
        DUMMY_TYPED_DICT_NAME,
        {
            "required_fields": TypedDict(
                DUMMY_REQUIRED_TYPED_DICT_NAME, required_fields
            ),
            "optional_fields": TypedDict(
                DUMMY_OPTIONAL_TYPED_DICT_NAME, optional_fields
            ),
        },
    )


def field_annotations(typed_dict) -> Tuple[Dict[str, type], Dict[str, type]]:
    """Return the required and optional fields in the TypedDict."""
    return (
        typed_dict.__annotations__["required_fields"].__annotations__,
        typed_dict.__annotations__["optional_fields"].__annotations__,
    )


def is_anonymous_typed_dict(typ: type) -> bool:
    """Return true if this is an anonymous TypedDict as generated by MonkeyType."""
    return is_typed_dict(typ) and typ.__name__ == DUMMY_TYPED_DICT_NAME


def shrink_typed_dict_types(typed_dicts: List[type], max_typed_dict_size: int) -> type:
    """Shrink a list of TypedDicts into one with the required fields and the optional fields.
    Required fields are keys that appear as a required field in all the TypedDicts.
    Optional fields are those that appear as a required field in only some
    of the TypedDicts or appear as a optional field in even one TypedDict.
    If the same key has multiple value types, then its value is the Union of the value types.
    """
    num_typed_dicts = len(typed_dicts)
    key_value_types_dict = defaultdict(list)
    existing_optional_fields = []
    for typed_dict in typed_dicts:
        required_fields, optional_fields = field_annotations(typed_dict)
        for key, value_type in required_fields.items():
            key_value_types_dict[key].append(value_type)
        existing_optional_fields.extend(optional_fields.items())

    required_fields = {
        key: value_types
        for key, value_types in key_value_types_dict.items()
        if len(value_types) == num_typed_dicts
    }
    optional_fields = defaultdict(list)
    for key, value_types in key_value_types_dict.items():
        if len(value_types) != num_typed_dicts:
            optional_fields[key] = value_types
    for key, value_type in existing_optional_fields:
        optional_fields[key].append(value_type)

    if len(required_fields) + len(optional_fields) > max_typed_dict_size:
        value_type = shrink_types(
            list(
                chain.from_iterable(
                    chain(required_fields.values(), optional_fields.values())
                )
            ),
            max_typed_dict_size,
        )
        return dict[str, value_type]
    required_fields = {
        key: shrink_types(list(value_types), max_typed_dict_size)
        for key, value_types in required_fields.items()
    }
    optional_fields = {
        key: shrink_types(list(value_types), max_typed_dict_size)
        for key, value_types in optional_fields.items()
    }
    return make_typed_dict(
        required_fields=required_fields, optional_fields=optional_fields
    )


def shrink_types(types, max_typed_dict_size):
    """Return the smallest type equivalent to Union[types].
    If all the types are anonymous TypedDicts, shrink them ourselves.
    Otherwise, recursively turn the anonymous TypedDicts into Dicts.
    Union will handle deduplicating types (both by equality and subtype relationships).
    """
    types = tuple(types)
    if len(types) == 0:
        return Any
    if all(is_anonymous_typed_dict(typ) for typ in types):
        return shrink_typed_dict_types(types, max_typed_dict_size)
    # Don't rewrite anonymous TypedDict to Dict if the types are all the same,
    # such as [Tuple[TypedDict(...)], Tuple[TypedDict(...)]].
    if all(types_equal(typ, types[0]) for typ in types[1:]):
        return types[0]

    # If they are all lists, shrink their argument types. This way, we avoid
    # rewriting heterogeneous anonymous TypedDicts to Dict.
    if all(is_list(typ) for typ in types):
        annotation = shrink_types(
            (getattr(typ, "__args__")[0] for typ in types), max_typed_dict_size
        )
        return List[annotation]

    all_dict_types = tuple(
        RewriteAnonymousTypedDictToDict().rewrite(typ) for typ in types
    )
    return Union[all_dict_types]


def make_iterator(typ):
    return Iterator[typ]


def make_generator(yield_typ, send_typ, return_typ):
    return Generator[yield_typ, send_typ, return_typ]


_BUILTIN_CALLABLE_TYPES = (
    types.FunctionType,
    types.LambdaType,
    types.MethodType,
    types.BuiltinMethodType,
    types.BuiltinFunctionType,
)


def get_dict_type(dct, max_typed_dict_size):
    """Return a TypedDict for `dct` if all the keys are strings.
    Else, default to the union of the keys and of the values."""
    if len(dct) == 0:
        # Special-case this because returning an empty TypedDict is
        # unintuitive, especially when you've "disabled" TypedDict generation
        # by setting `max_typed_dict_size` to 0.
        return Dict[Any, Any]
    if all(isinstance(k, str) for k in dct.keys()) and (
        max_typed_dict_size is None or len(dct) <= max_typed_dict_size
    ):
        return make_typed_dict(
            required_fields={
                k: get_type(v, max_typed_dict_size) for k, v in dct.items()
            }
        )
    else:
        key_type = shrink_types(
            (get_type(k, max_typed_dict_size) for k in dct.keys()), max_typed_dict_size
        )
        val_type = shrink_types(
            (get_type(v, max_typed_dict_size) for v in dct.values()),
            max_typed_dict_size,
        )
        return Dict[key_type, val_type]


def get_type(obj, max_typed_dict_size):
    """Return the static type that would be used in a type hint"""
    if isinstance(obj, type):
        return type[obj]
    elif isinstance(obj, _BUILTIN_CALLABLE_TYPES):
        return Callable
    elif isinstance(obj, types.GeneratorType):
        return Iterator[Any]
    typ = type(obj)
    if typ is list:
        elem_type = shrink_types(
            (get_type(e, max_typed_dict_size) for e in obj), max_typed_dict_size
        )
        return list[elem_type]
    elif typ is set:
        elem_type = shrink_types(
            (get_type(e, max_typed_dict_size) for e in obj), max_typed_dict_size
        )
        return set[elem_type]
    elif typ is dict:
        return get_dict_type(obj, max_typed_dict_size)
    elif typ is defaultdict:
        key_type = shrink_types(
            (get_type(k, max_typed_dict_size) for k in obj.keys()), max_typed_dict_size
        )
        val_type = shrink_types(
            (get_type(v, max_typed_dict_size) for v in obj.values()),
            max_typed_dict_size,
        )
        return DefaultDict[key_type, val_type]
    elif typ is tuple:
        return tuple[tuple(get_type(e, max_typed_dict_size) for e in obj)]
    return typ


NoneType = type(None)
NotImplementedType = type(NotImplemented)
mappingproxy = type(range.__dict__)


T = TypeVar("T")


class GenericTypeRewriter(Generic[T], ABC):
    @abstractmethod
    def make_builtin_tuple(self, elements): ...

    @abstractmethod
    def make_container_type(self, container_type, element): ...

    @abstractmethod
    def make_anonymous_typed_dict(self, required_fields, optional_fields): ...

    @abstractmethod
    def make_builtin_typed_dict(self, name, annotations, total): ...

    @abstractmethod
    def generic_rewrite(self, typ): ...

    @abstractmethod
    def rewrite_container_type(self, container_type): ...

    @abstractmethod
    def rewrite_malformed_container(self, container): ...

    @abstractmethod
    def rewrite_type_variable(self, type_variable): ...

    def _rewrite_container(self, cls, container):
        if container.__module__ != "typing":
            return self.rewrite_malformed_container(container)
        args = getattr(container, "__args__", None)
        if args is None:
            return self.rewrite_malformed_container(container)
        elif args == ((),):  # special case of empty tuple `Tuple[()]`
            elems = self.make_builtin_tuple(())
        else:
            elems = self.make_builtin_tuple(
                self.rewrite(elem) for elem in container.__args__
            )
        return self.make_container_type(self.rewrite_container_type(cls), elems)

    def rewrite_Dict(self, dct):
        return self._rewrite_container(dict, dct)

    def rewrite_List(self, lst):
        return self._rewrite_container(list, lst)

    def rewrite_Set(self, st):
        return self._rewrite_container(set, st)

    def rewrite_Tuple(self, tup):
        return self._rewrite_container(tuple, tup)

    def rewrite_Generator(self, generator):
        return self._rewrite_container(Generator, generator)

    def rewrite_anonymous_TypedDict(self, typed_dict):
        assert is_anonymous_typed_dict(typed_dict)
        required_fields, optional_fields = field_annotations(typed_dict)
        return self.make_anonymous_typed_dict(
            required_fields={
                name: self.rewrite(typ) for name, typ in required_fields.items()
            },
            optional_fields={
                name: self.rewrite(typ) for name, typ in optional_fields.items()
            },
        )

    def rewrite_TypedDict(self, typed_dict):
        if is_anonymous_typed_dict(typed_dict):
            return self.rewrite_anonymous_TypedDict(typed_dict)
        return self.make_builtin_typed_dict(
            typed_dict.__name__,
            {
                name: self.rewrite(typ)
                for name, typ in typed_dict.__annotations__.items()
            },
            total=typed_dict.__total__,
        )

    def rewrite_Union(self, union):
        args = get_args(union)
        union_new = args[0]
        for t in args[1:]:
            union_new = union_new | t
        return self._rewrite_container(UnionType, union_new)

    def rewrite(self, typ):
        if is_any(typ):
            typname = "Any"
        elif is_union(typ):
            typname = "Union"
        elif is_typed_dict(typ):
            typname = "TypedDict"
        elif is_generic(typ):
            typname = name_of_generic(typ)
        else:
            typname = getattr(typ, "__name__", None)
        rewriter = getattr(self, "rewrite_" + typname, None) if typname else None
        if rewriter:
            return rewriter(typ)
        if isinstance(typ, TypeVar):
            return self.rewrite_type_variable(typ)
        return self.generic_rewrite(typ)


class TypeRewriter(GenericTypeRewriter[type]):
    """TypeRewriter provides a visitor for rewriting parts of types"""

    def make_anonymous_typed_dict(self, required_fields, optional_fields):
        return make_typed_dict(
            required_fields=required_fields, optional_fields=optional_fields
        )

    def make_builtin_typed_dict(self, name, annotations, total):
        return TypedDict(name, annotations, total=total)

    def generic_rewrite(self, typ):
        return typ

    def rewrite_container_type(self, container_type):
        return container_type

    def rewrite_malformed_container(self, container):
        return container

    def rewrite_type_variable(self, type_variable):
        return type_variable

    def make_builtin_tuple(self, elements):
        return tuple(elements)

    def make_container_type(self, container_type, element):
        return container_type[element]


class RemoveEmptyContainers(TypeRewriter):
    """Remove redundant, empty containers from union types.

    Empty containers are typed as C[Any] by MonkeyType. They should be removed
    if there is a single concrete, non-null type in the Union. For example,

        Union[Set[Any], Set[int]] -> Set[int]

    Union[] handles the case where there is only a single type left after
    removing the empty container.
    """

    def _is_empty(self, typ):
        args = getattr(typ, "__args__", [])
        return args and all(is_any(e) for e in args)

    def rewrite_Union(self, union):
        elems = tuple(self.rewrite(e) for e in union.__args__ if not self._is_empty(e))
        if elems:
            args = get_args(union)
            union_new = args[0]
            for t in args[1:]:
                union_new = union_new | t
            return union_new
        return union


class RewriteConfigDict(TypeRewriter):
    """Union[Dict[K, V1], ..., Dict[K, VN]] -> Dict[K, Union[V1, ..., VN]]"""

    def rewrite_Union(self, union):
        key_type = None
        value_types = []
        for e in union.__args__:
            if not is_generic_of(e, Dict):
                return union
            key_type = key_type or e.__args__[0]
            if key_type != e.__args__[0]:
                return union
            value_types.extend(e.__args__[1:])
        return Dict[key_type, Union[tuple(value_types)]]


class RewriteLargeUnion(TypeRewriter):
    """Rewrite Union[T1, ..., TN] as Any for large N."""

    def __init__(self, max_union_len: int = 5):
        super().__init__()
        self.max_union_len = max_union_len

    def _rewrite_to_tuple(self, union):
        """Union[Tuple[V, ..., V], Tuple[V, ..., V], ...] -> Tuple[V, ...]"""
        value_type = None
        for t in union.__args__:
            if not is_generic_of(t, Tuple):
                return None
            value_type = value_type or t.__args__[0]
            if not all(vt is value_type for vt in t.__args__):
                return None
        return Tuple[value_type, ...]

    def rewrite_Union(self, union):
        if len(union.__args__) <= self.max_union_len:
            return union

        rw_union = self._rewrite_to_tuple(union)
        if rw_union is not None:
            return rw_union

        try:
            for ancestor in inspect.getmro(union.__args__[0]):
                if ancestor is not object and all(
                    issubclass(t, ancestor) for t in union.__args__
                ):
                    return ancestor
        except (TypeError, AttributeError):
            pass
        return Any


class RewriteAnonymousTypedDictToDict(TypeRewriter):
    """TypedDict('Foo', {"k": v1, ...}) -> Dict[str, Union[v1, ...]]."""

    def rewrite_anonymous_TypedDict(self, typed_dict):
        assert is_anonymous_typed_dict(typed_dict)
        required_fields, optional_fields = field_annotations(typed_dict)
        all_value_types = [*required_fields.values(), *optional_fields.values()]
        if not all_value_types:
            # Special-case this because we can't justify any type.
            return Dict[Any, Any]
        return Dict[str, Union[tuple(self.rewrite(typ) for typ in all_value_types)]]


class ChainedRewriter(TypeRewriter):
    def __init__(self, rewriters: Iterable[TypeRewriter]) -> None:
        self.rewriters = rewriters

    def rewrite(self, typ):
        for rw in self.rewriters:
            typ = rw.rewrite(typ)
        return typ


class NoOpRewriter(TypeRewriter):
    def rewrite(self, typ):
        return typ


class RewriteGenerator(TypeRewriter):
    """Returns an Iterator, if the send_type and return_type of a Generator is None"""

    def rewrite_Generator(self, typ):
        args = typ.__args__
        if args[1] is NoneType and args[2] is NoneType:
            return Iterator[args[0]]
        return typ


class RewriteMostSpecificCommonBase(TypeRewriter):
    """
    Relace a union of classes by the most specific
    common base of its members (while avoiding multiple
    inheritance), i.e.,

    Union[Derived1, Derived2] -> Base
    """

    def _compute_bases(self, klass):
        """
        Return list of bases of a given class,
        going from general (i.e., closer to object)
        to specific (i.e., closer to class).
        The list ends with the class itself, its
        first element is the most general base of
        the class up to (but excluding) any
        base class having multiple inheritance
        or the object class itself.
        """
        bases = []

        curr_klass = klass

        while curr_klass is not object:
            bases.append(curr_klass)

            curr_bases = curr_klass.__bases__

            if len(curr_bases) != 1:
                break

            curr_klass = curr_bases[0]
        return bases[::-1]

    def _merge_common_bases(self, first_bases, second_bases):
        """
        Return list of bases common to both* classes,
        going from general (i.e., closer to object)
        to specific (i.e., closer to both classes).
        """
        merged_bases = []

        # Only process up to shorter of the lists
        for first_base, second_base in zip(first_bases, second_bases):
            if first_base is second_base:
                merged_bases.append(second_base)
            else:
                break

        return merged_bases

    def rewrite_Union(self, union):
        """
        Rewrite the union if possible, if no meaningful rewrite is possible,
        return the original union.
        """
        klasses = union.__args__

        all_bases = []

        for klass in klasses:
            klass_bases = self._compute_bases(klass)
            all_bases.append(klass_bases)

        common_bases = functools.reduce(self._merge_common_bases, all_bases)

        if common_bases:
            return common_bases[-1]
        return union


DEFAULT_REWRITER = ChainedRewriter(
    (
        RemoveEmptyContainers(),
        RewriteConfigDict(),
        RewriteLargeUnion(),
        RewriteGenerator(),
    )
)
