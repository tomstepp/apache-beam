#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Module to convert Python's native typing types to Beam types."""

# pytype: skip-file

import collections
import collections.abc
import logging
import sys
import types
import typing
from typing import Generic
from typing import TypeVar

from apache_beam.typehints import typehints

T = TypeVar('T')

_LOGGER = logging.getLogger(__name__)

# Describes an entry in the type map in convert_to_beam_type.
# match is a function that takes a user type and returns whether the conversion
# should trigger.
# arity is the expected arity of the user type. -1 means it's variadic.
# beam_type is the Beam type the user type should map to.
_TypeMapEntry = collections.namedtuple(
    '_TypeMapEntry', ['match', 'arity', 'beam_type'])

_BUILTINS_TO_TYPING = {
    dict: typing.Dict,
    list: typing.List,
    tuple: typing.Tuple,
    set: typing.Set,
    frozenset: typing.FrozenSet,
}

_BUILTINS = [
    dict,
    list,
    tuple,
    set,
    frozenset,
]

_CONVERTED_COLLECTIONS = [
    collections.abc.Iterable,
    collections.abc.Iterator,
    collections.abc.Generator,
    collections.abc.Set,
    collections.abc.MutableSet,
    collections.abc.Collection,
    collections.abc.Sequence,
    collections.abc.Mapping,
]

_CONVERTED_MODULES = ('typing', 'collections', 'collections.abc')


def _get_args(typ):
  """Returns a list of arguments to the given type.

  Args:
    typ: A typing module typing type.

  Returns:
    A tuple of args.
  """
  try:
    if typ.__args__ is None:
      return ()
    return typ.__args__
  except AttributeError:
    if isinstance(typ, typing.TypeVar):
      return (typ.__name__, )
    return ()


def _safe_issubclass(derived, parent):
  """Like issubclass, but swallows TypeErrors.

  This is useful for when either parameter might not actually be a class,
  e.g. typing.Union isn't actually a class.

  Args:
    derived: As in issubclass.
    parent: As in issubclass.

  Returns:
    issubclass(derived, parent), or False if a TypeError was raised.
  """
  try:
    return issubclass(derived, parent)
  except (TypeError, AttributeError):
    if hasattr(derived, '__origin__'):
      try:
        return issubclass(derived.__origin__, parent)
      except TypeError:
        pass
    return False


def _match_issubclass(match_against):
  return lambda user_type: _safe_issubclass(user_type, match_against)


def _is_primitive(user_type, primitive):
  # catch bare primitives
  if user_type is primitive:
    return True
  return getattr(user_type, '__origin__', None) is primitive


def _match_is_primitive(match_against):
  return lambda user_type: _is_primitive(user_type, match_against)


def _match_is_dict(user_type):
  return _is_primitive(user_type, dict) or _safe_issubclass(user_type, dict)


def _match_is_exactly_mapping(user_type):
  # Avoid unintentionally catching all subtypes (e.g. strings and mappings).
  expected_origin = collections.abc.Mapping
  return getattr(user_type, '__origin__', None) is expected_origin


def _match_is_exactly_iterable(user_type):
  if user_type is typing.Iterable or user_type is collections.abc.Iterable:
    return True
  # Avoid unintentionally catching all subtypes (e.g. strings and mappings).
  expected_origin = collections.abc.Iterable
  return getattr(user_type, '__origin__', None) is expected_origin


def _match_is_exactly_collection(user_type):
  return getattr(user_type, '__origin__', None) is collections.abc.Collection


def _match_is_exactly_sequence(user_type):
  return getattr(user_type, '__origin__', None) is collections.abc.Sequence


def match_is_named_tuple(user_type):
  return (
      _safe_issubclass(user_type, typing.Tuple) and
      hasattr(user_type, '__annotations__'))


def _match_is_optional(user_type):
  return _match_is_union(user_type) and sum(
      tp is type(None) for tp in _get_args(user_type)) == 1


def extract_optional_type(user_type):
  """Extracts the non-None type from Optional type user_type.

  If user_type is not Optional, returns None
  """
  if not _match_is_optional(user_type):
    return None
  else:
    return next(tp for tp in _get_args(user_type) if tp is not type(None))


def _match_is_union(user_type):
  # For non-subscripted unions (Python 2.7.14+ with typing 3.64)
  if user_type is typing.Union:
    return True

  try:  # Python 3.5.4+, or Python 2.7.14+ with typing 3.64
    return user_type.__origin__ is typing.Union
  except AttributeError:
    pass

  return False


def _match_is_set(user_type):
  if _safe_issubclass(user_type, typing.Set) or _is_primitive(user_type, set):
    return True
  elif getattr(user_type, '__origin__', None) is not None:
    return _safe_issubclass(
        user_type.__origin__, collections.abc.Set) or _safe_issubclass(
            user_type.__origin__, collections.abc.MutableSet)
  else:
    return False


def is_any(typ):
  return typ is typing.Any


def is_new_type(typ):
  return hasattr(typ, '__supertype__')


_ForwardRef = typing.ForwardRef  # Python 3.7+


def is_forward_ref(typ):
  return isinstance(typ, _ForwardRef)


# Mapping from typing.TypeVar/typehints.TypeVariable ids to an object of the
# other type. Bidirectional mapping preserves typing.TypeVar instances.
_type_var_cache: typing.Dict[int, typehints.TypeVariable] = {}


def convert_builtin_to_typing(typ):
  """Convert recursively a given builtin to a typing object.

  Args:
    typ (`builtins`): builtin object that exist in _BUILTINS_TO_TYPING.

  Returns:
    type: The given builtins converted to a type.

  """
  if getattr(typ, '__origin__', None) in _BUILTINS_TO_TYPING:
    args = map(convert_builtin_to_typing, typ.__args__)
    typ = _BUILTINS_TO_TYPING[typ.__origin__].copy_with(tuple(args))
  return typ


def convert_typing_to_builtin(typ):
  """Converts a given typing collections type to its builtin counterpart.

  Args:
    typ: A typing type (e.g., typing.List[int]).

  Returns:
    type: The corresponding builtin type (e.g., list[int]).
  """
  origin = getattr(typ, '__origin__', None)
  args = getattr(typ, '__args__', None)
  # Typing types return the primitive type as the origin from 3.9 on
  if origin not in _BUILTINS:
    return typ
  # Early return for bare types
  if not args:
    return origin
  if origin is list:
    return list[convert_typing_to_builtin(args[0])]
  elif origin is dict:
    return dict[convert_typing_to_builtin(args[0]),
                convert_typing_to_builtin(args[1])]
  elif origin is tuple:
    return tuple[tuple(convert_typing_to_builtin(args))]
  elif origin is set:
    return set[convert_typing_to_builtin(args)]
  elif origin is frozenset:
    return frozenset[convert_typing_to_builtin(args)]


def convert_collections_to_typing(typ):
  """Converts a given collections.abc type to a typing object.

  Args:
    typ: an object inheriting from a collections.abc object

  Returns:
    type: The corresponding typing object.
  """
  if hasattr(typ, '__iter__'):
    if hasattr(typ, '__next__'):
      typ = typing.Iterator[typ.__args__]
    elif hasattr(typ, 'send') and hasattr(typ, 'throw'):
      typ = typing.Generator[typ.__args__]
    elif _match_is_exactly_iterable(typ):
      typ = typing.Iterable[typ.__args__]
  return typ


def is_builtin(typ):
  if typ in _BUILTINS:
    return True
  return getattr(typ, '__origin__', None) in _BUILTINS


# During type inference of WindowedValue, we need to pass in the inner value
# type. This cannot be achieved immediately with WindowedValue class because it
# is not parameterized. Changing it to a generic class (e.g. WindowedValue[T])
# could work in theory. However, the class is cythonized and it seems that
# cython does not handle generic classes well.
# The workaround here is to create a separate class solely for the type
# inference purpose. This class should never be used for creating instances.
class TypedWindowedValue(Generic[T]):
  def __init__(self, *args, **kwargs):
    raise NotImplementedError("This class is solely for type inference")


def convert_to_beam_type(typ):
  """Convert a given typing type to a Beam type.

  Args:
    typ (`typing.Union[type, str]`): typing type or string literal representing
      a type.

  Returns:
    type: The given type converted to a Beam type as far as we can do the
    conversion.

  Raises:
    ValueError: The type was malformed.
  """
  # Convert `int | float` to typing.Union[int, float]
  # pipe operator as Union and types.UnionType are introduced
  # in Python 3.10.
  # GH issue: https://github.com/apache/beam/issues/21972
  if (sys.version_info.major == 3 and
      sys.version_info.minor >= 10) and (isinstance(typ, types.UnionType)):
    typ = typing.Union[typ]

  if getattr(typ, '__module__', None) == 'typing':
    typ = convert_typing_to_builtin(typ)

  typ_module = getattr(typ, '__module__', None)
  if isinstance(typ, typing.TypeVar):
    # This is a special case, as it's not parameterized by types.
    # Also, identity must be preserved through conversion (i.e. the same
    # TypeVar instance must get converted into the same TypeVariable instance).
    # A global cache should be OK as the number of distinct type variables
    # is generally small.
    if id(typ) not in _type_var_cache:
      new_type_variable = typehints.TypeVariable(typ.__name__)
      _type_var_cache[id(typ)] = new_type_variable
      _type_var_cache[id(new_type_variable)] = typ
    return _type_var_cache[id(typ)]
  elif isinstance(typ, str):
    # Special case for forward references.
    # TODO(https://github.com/apache/beam/issues/19954): Currently unhandled.
    _LOGGER.info('Converting string literal type hint to Any: "%s"', typ)
    return typehints.Any
  elif sys.version_info >= (3, 10) and isinstance(typ, typing.NewType):  # pylint: disable=isinstance-second-argument-not-valid-type
    # Special case for NewType, where, since Python 3.10, NewType is now a class
    # rather than a function.
    # TODO(https://github.com/apache/beam/issues/20076): Currently unhandled.
    _LOGGER.info('Converting NewType type hint to Any: "%s"', typ)
    return typehints.Any
  elif typ_module == 'apache_beam.typehints.native_type_compatibility' and \
      getattr(typ, "__name__", typ.__origin__.__name__) == 'TypedWindowedValue':
    # Need to pass through WindowedValue class so that it can be converted
    # to the correct type constraint in Beam
    # This is needed to fix https://github.com/apache/beam/issues/33356
    pass

  elif typ_module not in _CONVERTED_MODULES and not is_builtin(typ):
    # Only translate primitives and types from collections.abc and typing.
    return typ
  if (typ_module == 'collections.abc' and
      getattr(typ, '__origin__', typ) not in _CONVERTED_COLLECTIONS):
    # TODO(https://github.com/apache/beam/issues/29135):
    # Support more collections types
    return typ

  type_map = [
      # TODO(https://github.com/apache/beam/issues/20076): Currently
      # unsupported.
      _TypeMapEntry(match=is_new_type, arity=0, beam_type=typehints.Any),
      # TODO(https://github.com/apache/beam/issues/19954): Currently
      # unsupported.
      _TypeMapEntry(match=is_forward_ref, arity=0, beam_type=typehints.Any),
      _TypeMapEntry(match=is_any, arity=0, beam_type=typehints.Any),
      _TypeMapEntry(match=_match_is_dict, arity=2, beam_type=typehints.Dict),
      _TypeMapEntry(
          match=_match_is_exactly_iterable,
          arity=1,
          beam_type=typehints.Iterable),
      _TypeMapEntry(
          match=_match_is_primitive(list), arity=1, beam_type=typehints.List),
      # FrozenSets are a specific instance of a set, so we check this first.
      _TypeMapEntry(
          match=_match_is_primitive(frozenset),
          arity=1,
          beam_type=typehints.FrozenSet),
      _TypeMapEntry(match=_match_is_set, arity=1, beam_type=typehints.Set),
      # NamedTuple is a subclass of Tuple, but it needs special handling.
      # We just convert it to Any for now.
      # This MUST appear before the entry for the normal Tuple.
      _TypeMapEntry(
          match=match_is_named_tuple, arity=0, beam_type=typehints.Any),
      _TypeMapEntry(
          match=_match_is_primitive(tuple), arity=-1,
          beam_type=typehints.Tuple),
      _TypeMapEntry(match=_match_is_union, arity=-1, beam_type=typehints.Union),
      _TypeMapEntry(
          match=_match_issubclass(collections.abc.Generator),
          arity=3,
          beam_type=typehints.Generator),
      _TypeMapEntry(
          match=_match_issubclass(collections.abc.Iterator),
          arity=1,
          beam_type=typehints.Iterator),
      _TypeMapEntry(
          match=_match_is_exactly_collection,
          arity=1,
          beam_type=typehints.Collection),
      _TypeMapEntry(
          match=_match_issubclass(TypedWindowedValue),
          arity=1,
          beam_type=typehints.WindowedValue),
      _TypeMapEntry(
          match=_match_is_exactly_sequence,
          arity=1,
          beam_type=typehints.Sequence),
      _TypeMapEntry(
          match=_match_is_exactly_mapping, arity=2,
          beam_type=typehints.Mapping),
  ]

  # Find the first matching entry.
  matched_entry = next((entry for entry in type_map if entry.match(typ)), None)
  if not matched_entry:
    # Please add missing type support if you see this message.
    _LOGGER.info('Using Any for unsupported type: %s', typ)
    return typehints.Any

  args = _get_args(typ)
  len_args = len(args)
  if len_args == 0 and len_args != matched_entry.arity:
    arity = matched_entry.arity
    # Handle unsubscripted types.
    if _match_issubclass(typing.Tuple)(typ):
      args = (typehints.TypeVariable('T'), Ellipsis)
    elif _match_is_union(typ):
      raise ValueError('Unsupported Union with no arguments.')
    elif _match_issubclass(typing.Generator)(typ):
      # Assume a simple generator.
      args = (typehints.TypeVariable('T_co'), type(None), type(None))
    elif _match_issubclass(typing.Dict)(typ):
      args = (typehints.TypeVariable('KT'), typehints.TypeVariable('VT'))
    elif (_match_issubclass(typing.Iterator)(typ) or
          _match_is_exactly_iterable(typ)):
      args = (typehints.TypeVariable('T_co'), )
    else:
      args = (typehints.TypeVariable('T'), ) * arity
  elif matched_entry.arity == -1:
    arity = len_args
  # Counters are special dict types that are implicitly parameterized to
  # [T, int], so we fix cases where they only have one argument to match
  # a more traditional dict hint.
  elif len_args == 1 and _safe_issubclass(getattr(typ, '__origin__', typ),
                                          collections.Counter):
    args = (args[0], int)
    len_args = 2
    arity = matched_entry.arity
  else:
    arity = matched_entry.arity
    if len_args != arity:
      raise ValueError(
          'expecting type %s to have arity %d, had arity %d '
          'instead' % (str(typ), arity, len_args))
  typs = convert_to_beam_types(args)
  if arity == 0:
    # Nullary types (e.g. Any) don't accept empty tuples as arguments.
    return matched_entry.beam_type
  elif arity == 1:
    # Unary types (e.g. Set) don't accept 1-tuples as arguments
    return matched_entry.beam_type[typs[0]]
  else:
    return matched_entry.beam_type[tuple(typs)]


def convert_to_beam_types(args):
  """Convert the given list or dictionary of args to Beam types.

  Args:
    args: Either an iterable of types, or a dictionary where the values are
    types.

  Returns:
    If given an iterable, a list of converted types. If given a dictionary,
    a dictionary with the same keys, and values which have been converted.
  """
  if isinstance(args, dict):
    return {k: convert_to_beam_type(v) for k, v in args.items()}
  else:
    return [convert_to_beam_type(v) for v in args]


def convert_to_python_type(typ):
  """Converts a given Beam type to a python type.

  This is the reverse of convert_to_beam_type.

  Args:
    typ: If a typehints.TypeConstraint, the type to convert. Otherwise, typ
      will be unchanged.

  Returns:
    Converted version of typ, or unchanged.

  Raises:
    ValueError: The type was malformed or could not be converted.
  """
  if isinstance(typ, typehints.TypeVariable):
    # This is a special case, as it's not parameterized by types.
    # Also, identity must be preserved through conversion (i.e. the same
    # TypeVariable instance must get converted into the same TypeVar instance).
    # A global cache should be OK as the number of distinct type variables
    # is generally small.
    if id(typ) not in _type_var_cache:
      new_type_variable = typing.TypeVar(typ.name)
      _type_var_cache[id(typ)] = new_type_variable
      _type_var_cache[id(new_type_variable)] = typ
    return _type_var_cache[id(typ)]
  elif not getattr(typ, '__module__', None).endswith('typehints'):
    # Only translate types from the typehints module.
    return typ

  if isinstance(typ, typehints.AnyTypeConstraint):
    return typing.Any
  if isinstance(typ, typehints.DictConstraint):
    return dict[convert_to_python_type(typ.key_type),
                convert_to_python_type(typ.value_type)]
  if isinstance(typ, typehints.ListConstraint):
    return list[convert_to_python_type(typ.inner_type)]
  if isinstance(typ, typehints.IterableTypeConstraint):
    return collections.abc.Iterable[convert_to_python_type(typ.inner_type)]
  if isinstance(typ, typehints.UnionConstraint):
    if not typ.union_types:
      # Gracefully handle the empty union type.
      return typing.Any
    return typing.Union[tuple(convert_to_python_types(typ.union_types))]
  if isinstance(typ, typehints.SetTypeConstraint):
    return set[convert_to_python_type(typ.inner_type)]
  if isinstance(typ, typehints.FrozenSetTypeConstraint):
    return frozenset[convert_to_python_type(typ.inner_type)]
  if isinstance(typ, typehints.TupleConstraint):
    return tuple[tuple(convert_to_python_types(typ.tuple_types))]
  if isinstance(typ, typehints.TupleSequenceConstraint):
    return tuple[convert_to_python_type(typ.inner_type), ...]
  if isinstance(typ, typehints.ABCSequenceTypeConstraint):
    return collections.abc.Sequence[convert_to_python_type(typ.inner_type)]
  if isinstance(typ, typehints.IteratorTypeConstraint):
    return collections.abc.Iterator[convert_to_python_type(typ.yielded_type)]
  if isinstance(typ, typehints.MappingTypeConstraint):
    return collections.abc.Mapping[convert_to_python_type(typ.key_type),
                                   convert_to_python_type(typ.value_type)]

  raise ValueError('Failed to convert Beam type: %s' % typ)


def convert_to_python_types(args):
  """Convert the given list or dictionary of args to python types.

  Args:
    args: Either an iterable of types, or a dictionary where the values are
    types.

  Returns:
    If given an iterable, a list of converted types. If given a dictionary,
    a dictionary with the same keys, and values which have been converted.
  """
  if isinstance(args, dict):
    return {k: convert_to_python_type(v) for k, v in args.items()}
  else:
    return [convert_to_python_type(v) for v in args]
