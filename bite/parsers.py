import itertools
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Generic,
    Iterable,
    NoReturn,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

from typing_extensions import Protocol

from bite.io import ParserBuffer

T = TypeVar("T", covariant=True)
V = TypeVar("V", covariant=True)


class ParsedNode(Protocol[T, V]):
    @property
    def name(self) -> Optional[str]:
        ...

    @property
    def parse_tree(self) -> T:
        ...

    @property
    def values(self) -> Iterable[V]:
        ...

    @property
    def start_loc(self) -> int:
        ...

    @property
    def end_loc(self) -> int:
        ...


@dataclass(frozen=True)
class ParsedBaseNode(Generic[T]):
    name: Optional[str]
    parse_tree: T


@dataclass(frozen=True)
class ParsedLeaf(ParsedBaseNode[T]):
    name: Optional[str]
    parse_tree: T
    start_loc: int
    end_loc: int

    @property
    def values(self) -> Tuple[T]:
        return (self.parse_tree,)


@dataclass(frozen=True)
class ParsedNil:
    name: Optional[str]
    loc: int

    @property
    def parse_tree(self) -> None:
        return None

    @property
    def values(self) -> Tuple[()]:
        return ()

    @property
    def start_loc(self) -> int:
        return self.loc

    @property
    def end_loc(self) -> int:
        return self.loc


class Parser(Generic[T, V]):
    def __init__(self, name=None):
        self.name = name

    def __str__(self) -> str:
        return self.name if self.name else super().__str__()

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedNode[T, V]:
        raise NotImplementedError()

    def __add__(self, other: "Parser") -> "And":
        return And((self, other), name=f"({self}) + ({other})")

    def __or__(self, other: "Parser") -> "MatchFirst":
        return MatchFirst((self, other), name=f"({self}) | ({other})")

    def __invert__(self) -> "Not":
        return Not(self)

    def __getitem__(
        self, repeats: Union[int, Tuple[int, Union[int, "ellipsis", None]]]
    ) -> "Repeat":
        if isinstance(repeats, int):
            min_repeats = repeats
            max_repeats: Optional[int] = repeats
        else:
            min_repeats = repeats[0]
            max_repeats = repeats[1] if isinstance(repeats[1], int) else None
        return Repeat(
            self,
            min_repeats,
            max_repeats,
            name=f"({self})[{min_repeats}, {'...' if max_repeats is None else max_repeats}]",
        )


@dataclass(frozen=True)
class ParsedMatchFirst(ParsedBaseNode[ParsedNode[T, V]]):
    choice_index: int

    @property
    def values(self) -> Iterable[V]:
        return self.parse_tree.values

    @property
    def start_loc(self) -> int:
        return self.parse_tree.start_loc

    @property
    def end_loc(self) -> int:
        return self.parse_tree.end_loc


class MatchFirst(Parser[ParsedNode[Any, V], V]):
    """Apply the first parser that succeeds parsing the input.

    Parameters
    ----------
    choices:
        Parsers to try in the given order until one succeeds parsing the input.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: match-first

        import asyncio
        from bite import Literal, MatchFirst, parse_bytes

        print(asyncio.run(parse_bytes(
            MatchFirst([Literal(b'a'), Literal(b'b'), Literal(b'bb')]),
            b'bb'
        )).values)

    .. testoutput:: match-first

        (b'b',)
    """

    def __init__(self, choices: Iterable[Parser], *, name: str = None):
        super().__init__(name)
        self.choices = choices

    def __str__(self):
        return " | ".join(f"({choice})" for choice in self.choices)

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedMatchFirst:
        for i, choice in enumerate(self.choices):
            try:
                parsed_node = await choice.parse(buf, loc)
                return ParsedMatchFirst(self.name, parsed_node, i)
            except UnmetExpectationError:
                pass
        raise UnmetExpectationError(self, loc)

    def __or__(self, other: "Parser") -> "MatchFirst":
        return MatchFirst(tuple(self.choices) + (other,), name=f"{self} | ({other})")


@dataclass(frozen=True)
class ParsedList(ParsedBaseNode[Tuple[ParsedNode[T, V], ...]]):
    loc: int

    @property
    def values(self) -> Tuple[V, ...]:
        return tuple(
            itertools.chain.from_iterable(node.values for node in self.parse_tree)
        )

    @property
    def start_loc(self) -> int:
        if len(self.parse_tree) > 0:
            return self.parse_tree[0].start_loc
        else:
            return self.loc

    @property
    def end_loc(self) -> int:
        if len(self.parse_tree) > 0:
            return self.parse_tree[-1].end_loc
        else:
            return self.loc


ParsedAnd = ParsedList[Any, Any]


class And(Parser[Tuple[ParsedNode, ...], Any]):
    """Apply multiple parsers in sequence.

    Each parser must be able to parse the input when applied in sequence.

    Parameters
    ----------
    parsers:
        Parser to apply in sequence.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: and

        import asyncio
        from bite import And, Literal, parse_bytes

        print(asyncio.run(parse_bytes(And([Literal(b'a'), Literal(b'b')]), b'ab')).values)

    .. testoutput:: and

        (b'a', b'b')
    """

    def __init__(self, parsers: Iterable[Parser], *, name: str = None):
        super().__init__(name)
        self.parsers = parsers

    def __str__(self):
        return " + ".join(f"({parser})" for parser in self.parsers)

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedAnd:
        current_loc = loc
        parsed_nodes = []
        for parser in self.parsers:
            parsed_nodes.append(await parser.parse(buf, current_loc))
            current_loc = parsed_nodes[-1].end_loc
        return ParsedAnd(self.name, tuple(parsed_nodes), loc)

    def __add__(self, other: "Parser") -> "And":
        return And(tuple(self.parsers) + (other,), name=f"{self} + ({other})")


ParsedRepeat = ParsedList


class Repeat(Parser[Tuple[ParsedNode[T, V], ...], V]):
    """Apply a parser repeatedly.

    Parameters
    ----------
    parser:
        Parser to apply repeatedly.
    min_repeats:
        Minimun number of applications of the parser.
    max_repeats:
        Maximum number of applications of the parser. If ``None``, infinitly
        many applications are allowed.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: repeat

        import asyncio
        from bite import Literal, parse_bytes, Repeat

        repeat = Repeat(Literal(b'a'), min_repeats=1, max_repeats=2)

        print(asyncio.run(parse_bytes(repeat, b'')).values)

    .. testoutput:: repeat

        Traceback (most recent call last):
            ...
        bite.parsers.UnmetExpectationError: expected b'a' at position 0

    .. testcode:: repeat

        print(asyncio.run(parse_bytes(repeat, b'a')).values)

    .. testoutput:: repeat

        (b'a',)

    .. testcode:: repeat

        print(asyncio.run(parse_bytes(repeat, b'aa')).values)

    .. testoutput:: repeat

        (b'a', b'a')

    .. testcode:: repeat

        print(asyncio.run(parse_bytes(repeat, b'aaa')).values)

    .. testoutput:: repeat

        (b'a', b'a')
    """

    def __init__(
        self,
        parser: Parser[T, V],
        min_repeats: int = 0,
        max_repeats: int = None,
        *,
        name: str = None,
    ):
        super().__init__(name)
        self.parser = parser
        self.min_repeats = min_repeats
        self.max_repeats = max_repeats

    def __str__(self):
        return f"({self.parser})[{self.min_repeats}, {self.max_repeats}]"

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedRepeat:
        current_loc = loc
        parsed = []
        for _ in range(self.min_repeats):
            parsed.append(await self.parser.parse(buf, current_loc))
            current_loc = parsed[-1].end_loc

        for i in itertools.count(self.min_repeats):
            if self.max_repeats is not None and i >= self.max_repeats:
                break
            try:
                parsed.append(await self.parser.parse(buf, current_loc))
                current_loc = parsed[-1].end_loc
            except UnmetExpectationError:
                break

        return ParsedRepeat(self.name, tuple(parsed), loc)


class Not(Parser[None, NoReturn]):
    """Negative look-ahead.

    This parser does not consume any input bytes, but will only succeed parsing
    if the following input bytes are not parsed by the given parser.

    Parameters
    ----------
    parser:
        Parser that is supposed to not match the input.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: not

        import asyncio
        from bite import FixedByteCount, Literal, Not, parse_bytes

        expr = Not(Literal(b'a')) + FixedByteCount(1)

        print(asyncio.run(parse_bytes(expr, b'b')).values)

    .. testoutput:: not

        (b'b',)

    .. testcode:: not

        asyncio.run(parse_bytes(expr, b'a'))

    .. testoutput:: not

        Traceback (most recent call last):
            ...
        bite.parsers.UnmetExpectationError: expected Not(b'a') at position 0
    """

    def __init__(self, parser: Parser[Any, Any], *, name: str = None):
        super().__init__(name if name else f"Not({parser})")
        self.parser = parser

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedNil:
        try:
            await self.parser.parse(buf, loc)
        except UnmetExpectationError:
            return ParsedNil(self.name, loc)
        else:
            raise UnmetExpectationError(self, loc)


class Forward(Parser[T, V]):
    """Forward declaration allowing the definition of recursive rules.

    Use the :meth:`assign` method to set the actual parser definition.

    .. warning::

        Rules must not be left-recursive. Otherwise, the parser will
        recursively call itself causing a stack overflow.

    Parameters
    ----------
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: forward

        import asyncio
        from bite import Forward, Literal, Opt, parse_bytes

        expr = Forward()
        expr.assign(Literal(b'[') + Opt(expr) + Literal(b']'))

        print(asyncio.run(parse_bytes(expr, b'[[]]')).values)

    .. testoutput:: forward

        (b'[', b'[', b']', b']')
    """

    parser: Optional[Parser[T, V]]

    def __init__(self, *, name: str = None):
        super().__init__(name if name else "forward")
        self.parser = None

    def assign(self, parser: Parser[T, V]):
        """Assign a concrete parser to the forward declaration."""
        self.parser = parser

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedNode[T, V]:
        if self.parser is None:
            raise ValueError("unassigned forward parser")
        return await self.parser.parse(buf, loc)


ParsedLiteral = ParsedLeaf[bytes]


class Literal(Parser[bytes, bytes]):
    """Parses an exact sequence of bytes.

    Parameters
    ----------
    literal:
        The bytes to match.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: literal

        import asyncio
        from bite import Literal, parse_bytes

        print(asyncio.run(parse_bytes(Literal(b'abc'), b'abc')).values)

    .. testoutput:: literal

        (b'abc',)
    """

    def __init__(self, literal: bytes, *, name: str = None):
        super().__init__(name if name else str(literal))
        self.literal = literal

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedLiteral:
        end_loc = loc + len(self.literal)
        peek = await buf.get(slice(loc, end_loc))
        if peek == self.literal:
            return ParsedLiteral(self.name, self.literal, loc, end_loc)
        else:
            raise UnmetExpectationError(self, loc)


class CaselessLiteral(Parser[bytes, bytes]):
    """Parses a case-insensitive sequence of bytes.

    The *literal* passed to the :class:`CaselessLiteral` constructor will be
    treated as the canconical form, i.e. the value returned from the parse tree
    node.

    Parameters
    ----------
    literal:
        The canonical form of the bytes to match.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: caseless-literal

        import asyncio
        from bite import CaselessLiteral, parse_bytes

        print(asyncio.run(parse_bytes(CaselessLiteral(b'abc'), b'AbC')).values)

    .. testoutput:: caseless-literal

        (b'abc',)
    """

    def __init__(self, literal: bytes, *, name: str = None):
        super().__init__(name if name else str(literal))
        self.literal = literal
        self._lowercased_literal = self.literal.lower()

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedLiteral:
        end_loc = loc + len(self.literal)
        peek = await buf.get(slice(loc, end_loc))
        if peek.lower() == self._lowercased_literal:
            return ParsedLiteral(self.name, self.literal, loc, end_loc)
        else:
            raise UnmetExpectationError(self, loc)


ParsedCharacterSet = ParsedLeaf[bytes]


class CharacterSet(Parser[bytes, bytes]):
    """Parses a single byte from a given set.

    .. note::

        Besides listing each byte in the set explicitly (e.g. ``b'abc\x1F'``),
        you can define a range using something like
        ``bytes(range(0x7F, 0x9F + 1))``. It is also possible to combine both
        forms: ``b'abc\x1F' + bytes(range(0x7F, 0x9F + 1))``.

    Parameters
    ----------
    charset:
        The set of bytes parsed by this parser.
    invert:
        Set to ``true`` to match all bytes *not* given by the *charset*.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: character-set

        import asyncio
        from bite import CharacterSet, parse_bytes

        print(asyncio.run(parse_bytes(CharacterSet(b'abc'), b'b')).values)

    .. testoutput:: character-set

        (b'b',)
    """

    def __init__(
        self, charset: Iterable[int], *, invert: bool = False, name: str = None
    ):
        super().__init__(name if name else f"CharacterSet({charset})")
        self.charset = frozenset(charset)
        self.invert = invert

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedCharacterSet:
        char = await buf.get(loc)
        if len(char) == 1 and (char[0] in self.charset) != self.invert:
            return ParsedCharacterSet(self.name, char, loc, loc + 1)
        else:
            raise UnmetExpectationError(self, loc)


ParsedFixedByteCount = ParsedLeaf[bytes]


class FixedByteCount(Parser[bytes, bytes]):
    """Parses a fixed number of bytes.

    Parameters
    ----------
    count:
        How many bytes to read.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: fixed-byte-count

        import asyncio
        from bite import FixedByteCount, parse_bytes

        print(asyncio.run(parse_bytes(FixedByteCount(3), b'01234567890')).values)

    .. testoutput:: fixed-byte-count

        (b'012',)
    """

    def __init__(self, count: int, *, name: str = None):
        super().__init__(name if name else f"FixedByteCount({count})")
        self.count = count

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedFixedByteCount:
        read_bytes = await buf.get(slice(loc, loc + self.count))
        if len(read_bytes) == self.count:
            return ParsedFixedByteCount(
                self.name, read_bytes, loc, loc + len(read_bytes)
            )
        else:
            raise UnmetExpectationError(self, loc)


ParsedZeroOrMore = ParsedRepeat


class ZeroOrMore(Repeat[T, V]):
    """Require a parser to apply zero or more times.

    This is parser is equivalent to the :class:`Repeat` parser with
    ``min_repeats=0``.

    Parameters
    ----------
    parser:
        Parser for a single application.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: zero-or-more

        import asyncio
        from bite import Literal, parse_bytes, ZeroOrMore

        print(asyncio.run(parse_bytes(ZeroOrMore(Literal(b'a')), b'')).values)
        print(asyncio.run(parse_bytes(ZeroOrMore(Literal(b'a')), b'aaa')).values)

    .. testoutput:: zero-or-more

        ()
        (b'a', b'a', b'a')
    """

    def __init__(self, parser: Parser[T, V], *, name: str = None):
        super().__init__(parser, min_repeats=0, name=name)


ParsedOneOrMore = ParsedRepeat


class OneOrMore(Repeat[T, V]):
    """Require a parser to apply one or more times.

    This is parser is equivalent to the :class:`Repeat` parser with
    ``min_repeats=1``.

    Parameters
    ----------
    parser:
        Parser for a single application.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: one-or-more

        import asyncio
        from bite import Literal, OneOrMore, parse_bytes

        asyncio.run(parse_bytes(OneOrMore(Literal(b'a')), b''))

    .. testoutput:: one-or-more

        Traceback (most recent call last):
            ...
        bite.parsers.UnmetExpectationError: expected b'a' at position 0

    .. testcode:: one-or-more

        print(asyncio.run(parse_bytes(OneOrMore(Literal(b'a')), b'a')).values)
        print(asyncio.run(parse_bytes(OneOrMore(Literal(b'a')), b'aaa')).values)

    .. testoutput:: one-or-more

        (b'a',)
        (b'a', b'a', b'a')
    """

    def __init__(self, parser: Parser[T, V], *, name: str = None):
        super().__init__(parser, min_repeats=1, name=name)


ParsedOpt = ParsedRepeat


class Opt(Repeat[T, V]):
    """Make a parser optional.

    This is parser is equivalent to the :class:`Repeat` parser with
    ``min_repeats=0`` and ``max_repeats=1``.

    Parameters
    ----------
    parser:
        Parser to apply optionally.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: opt

        import asyncio
        from bite import Literal, Opt, parse_bytes

        print(asyncio.run(parse_bytes(Opt(Literal(b'a')), b'')).values)
        print(asyncio.run(parse_bytes(Opt(Literal(b'a')), b'a')).values)

    .. testoutput:: opt

        ()
        (b'a',)
    """

    def __init__(self, parser: Parser[T, V], *, name: str = None):
        super().__init__(parser, min_repeats=0, max_repeats=1, name=name)


@dataclass(frozen=True)
class CountedParseTree:
    count_expr: ParsedNode[Any, int]
    counted_expr: ParsedNode

    @property
    def start_loc(self) -> int:
        return self.count_expr.start_loc

    @property
    def end_loc(self) -> int:
        return self.counted_expr.end_loc


@dataclass(frozen=True)
class ParsedCounted(ParsedBaseNode[CountedParseTree], Generic[V]):
    @property
    def values(self) -> Iterable[V]:
        return self.parse_tree.counted_expr.values

    @property
    def start_loc(self) -> int:
        return self.parse_tree.start_loc

    @property
    def end_loc(self) -> int:
        return self.parse_tree.end_loc


class Counted(Parser[CountedParseTree, V]):
    """Read a count and create a parser from it.

    Parameters
    ----------
    count_parser:
        Parser to read the count. The resulting parse tree must return a single
        value that can be converted to an ``int``.
    counted_parser_factory:
        Callable that gets passed the count and returns a parser that is used
        to parse the subsequent bytes.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: counted

        import asyncio
        from bite import CharacterSet, Counted, FixedByteCount, parse_bytes

        print(asyncio.run(parse_bytes(
            Counted(
                CharacterSet(b'012345689'),
                lambda count: FixedByteCount(count)
            ),
            b'3abcde'
        )).values)

    .. testoutput:: counted

        (b'abc',)
    """

    def __init__(
        self,
        count_parser: Parser[Any, int],
        counted_parser_factory: Callable[[int], Parser[Any, V]],
        *,
        name: str = None,
    ):
        super().__init__(
            name if name else f"Counted({count_parser.name}, {counted_parser_factory})"
        )
        self.count_parser = count_parser
        self.counted_parser_factory = counted_parser_factory

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedCounted[V]:
        count_parse_tree = await self.count_parser.parse(buf, loc)
        values_iter = iter(count_parse_tree.values)
        try:
            count = int(next(values_iter))
        except StopIteration:
            raise ValueError("count expression did not return a value") from None
        try:
            next(values_iter)
        except StopIteration:
            counted = await self.counted_parser_factory(count).parse(
                buf, count_parse_tree.end_loc
            )
            return ParsedCounted(self.name, CountedParseTree(count_parse_tree, counted))
        else:
            raise ValueError("count expression returned more than one value")


ParsedCombine = ParsedLeaf[bytes]


class Combine(Parser[bytes, bytes]):
    """Combine parse tree leaves into a single node.

    This parser is helpful to obtain a single byte string when using multiple
    parsers producing individual segments of this byte string.

    Parameters
    ----------
    parser:
        Parser to obtain the individual segments to combine.
    name:
        Name to assign to the resulting parse tree node.

    Examples
    --------

    .. testcode:: combine

        import asyncio
        from bite import CharacterSet, Combine, parse_bytes

        digits = CharacterSet(b'0123456789')[1, ...]
        integer = Combine(digits)

        print(asyncio.run(parse_bytes(digits, b'12345')).values)
        print(asyncio.run(parse_bytes(integer, b'12345')).values)

    .. testoutput:: combine

        (b'1', b'2', b'3', b'4', b'5')
        (b'12345',)
    """

    def __init__(self, parser: Parser[Any, bytes], *, name: str = None):
        super().__init__(name if name else f"Combine({parser})")
        self.parser = parser

    async def parse(self, buf: ParserBuffer, loc: int = 0) -> ParsedCombine:
        parse_tree = await self.parser.parse(buf, loc)
        return ParsedCombine(
            self.name,
            b"".join(parse_tree.values),
            parse_tree.start_loc,
            parse_tree.end_loc,
        )


class ParseError(Exception):
    pass


class UnmetExpectationError(ParseError):
    def __init__(self, expected: Parser, at_loc: int):
        super().__init__(f"expected {expected} at position {at_loc}")
        self.expected = expected
        self.at_loc = at_loc


class TrailingBytesError(ParseError):
    pass


__all__ = [
    "And",
    "CaselessLiteral",
    "CharacterSet",
    "Combine",
    "Counted",
    "FixedByteCount",
    "Forward",
    "Literal",
    "MatchFirst",
    "Not",
    "OneOrMore",
    "Opt",
    "ParseError",
    "ParsedNode",
    "Parser",
    "Repeat",
    "Repeat",
    "TrailingBytesError",
    "UnmetExpectationError",
    "ZeroOrMore",
]
