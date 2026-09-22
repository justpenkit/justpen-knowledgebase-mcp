"""Typed exact predicates and conservative three-valued index evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import apsw

from .errors import InvalidParamsError
from .indexing import value_type
from .mutations import pointer_tokens, validate_properties
from .storage.graph_sql import PROPERTY_SELECT
from .text import tokenize, tokens

MISSING = object()


def resolve_pointer(document: object, pointer: str) -> object:
    """Resolve literal RFC6901 tokens using the actual container type."""
    value = document
    for component in pointer_tokens(pointer):
        if isinstance(value, dict):
            value = cast("dict[str, object]", value).get(component, MISSING)
        elif (
            isinstance(value, list)
            and component.isascii()
            and component.isdecimal()
            and (component == "0" or not component.startswith("0"))
        ):
            value = (
                cast("list[object]", value)[int(component)]
                if len(component) < 20 and int(component) < len(value)
                else MISSING
            )
        else:
            return MISSING
    return value


def strict_scalar_equal(left: object, right: object) -> bool:
    """Compare JSON scalars with exact Python integer/float comparison."""
    if left is MISSING or isinstance(left, (dict, list)):
        return False
    return value_type(left) == value_type(right) and left == right


def predicate_value(value: object, predicate: dict[str, Any]) -> bool:
    """Evaluate one leaf, including missing and strict mixed-type ranges."""
    op, operand = predicate["op"], predicate["value"]
    if op == "exists":
        return (value is not MISSING) == operand
    if value is MISSING:
        return False
    if op == "eq":
        return strict_scalar_equal(value, operand)
    if op == "ne":
        return not strict_scalar_equal(value, operand)
    if op == "in":
        return any(strict_scalar_equal(value, item) for item in operand)
    if value_type(value) not in ("number", "string") or value_type(value) != value_type(operand):
        return False
    comparable = cast("str | int | float", value)
    return {
        "gt": comparable > operand,
        "gte": comparable >= operand,
        "lt": comparable < operand,
        "lte": comparable <= operand,
    }[op]


def _validate_leaf(node: dict[str, Any]) -> None:
    if set(node) != {"path", "op", "value"} or type(node["path"]) is not str:
        raise ValueError("invalid predicate")
    node["path"].encode("utf-8")
    pointer_tokens(node["path"])
    op, value = node["op"], node["value"]
    if op not in ("eq", "ne", "in", "exists", "gt", "gte", "lt", "lte"):
        raise ValueError("invalid operator")
    if op == "exists":
        if type(value) is not bool:
            raise ValueError("exists requires boolean")
        return
    values = value if op == "in" else [value]
    if type(values) is not list:
        raise ValueError("in requires scalar list")
    scalars = cast("list[Any]", values)
    if not 1 <= len(scalars) <= 100:
        raise ValueError("in requires bounded scalar list")
    for scalar in scalars:
        if type(scalar) not in (str, int, float, bool, type(None)):
            raise ValueError("scalar required")
        validate_properties({"value": scalar})
        if op in ("gt", "gte", "lt", "lte") and type(scalar) not in (str, int, float):
            raise ValueError("range requires number or string")


def validate_filter(expression: dict[str, Any]) -> None:
    """Bound group depth, leaf count and scalar operands before compilation."""
    pending: list[tuple[object, int]] = [(expression, 0)]
    count = 0
    while pending:
        raw, depth = pending.pop()
        if type(raw) is not dict:
            raise ValueError("filter must be an object")
        node = cast("dict[str, Any]", raw)
        groups = set(node) & {"all", "any"}
        if groups:
            children = node[next(iter(groups))]
            if len(node) != 1 or depth >= 4 or type(children) is not list or not children:
                raise ValueError("invalid filter group or depth exceeds 4")
            pending.extend((child, depth + 1) for child in cast("list[Any]", children))
        else:
            count += 1
            if count > 32:
                raise ValueError("filter exceeds 32 predicates")
            _validate_leaf(node)


def evaluate(document: dict[str, Any], expression: dict[str, Any]) -> bool:
    """Evaluate the complete canonical AST without index assumptions."""
    if "all" in expression:
        return all(evaluate(document, child) for child in expression["all"])
    if "any" in expression:
        return any(evaluate(document, child) for child in expression["any"])
    return predicate_value(resolve_pointer(document, expression["path"]), expression)


class _Compiler:
    def __init__(self, kind: str) -> None:
        self.template = PROPERTY_SELECT[kind]
        self.parameters: list[Any] = []

    def bound(self, value: object) -> str:
        self.parameters.append(value)
        return "?"

    def comparison(self, op: str, operand: object) -> str:
        category = value_type(operand)
        match_type = self.bound(category)
        if op in ("eq", "ne", "in"):
            result = f"CASE WHEN p.value_type != {match_type} THEN 0 WHEN p.value_materialized=0 THEN NULL ELSE p.value IS {self.bound(operand)} END"
            return f"NOT ({result})" if op == "ne" else result
        operator = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        return f"CASE WHEN p.value_type != {match_type} THEN 0 WHEN p.value_materialized=0 THEN NULL ELSE p.value {operator} {self.bound(operand)} END"

    def compile_node(self, node: dict[str, Any]) -> str:
        for group, operator in (("all", " AND "), ("any", " OR ")):
            if group in node:
                return "(" + operator.join(self.compile_node(child) for child in node[group]) + ")"
        path, op, operand = node["path"], node["op"], node["value"]
        if len(pointer_tokens(path)) > 16:
            return str(int(op == "exists" and not operand))
        present = "EXISTS" + self.template.format(expression="1", condition="p.path=" + self.bound(path))
        if op == "exists":
            result = self.bound(int(operand))
        elif op == "in":
            result = "(" + " OR ".join(self.comparison("eq", item) for item in operand) + ")"
        else:
            result = self.comparison(op, operand)
        found = self.template.format(expression=result, condition="p.path=" + self.bound(path))
        ancestors = ["/" + "/".join(path.split("/")[1:index]) for index in range(2, len(path.split("/")))]
        scalar = "0"
        no_array = "1"
        if ancestors:
            values = ",".join(self.bound(item) for item in ancestors)
            scalar = "EXISTS" + self.template.format(
                expression="1", condition=f"p.path IN ({values}) AND p.value_type NOT IN ('object','array')"
            )
        complete = "json_extract(o.metadata,'$.property_index.paths_complete')=1"
        if ancestors:
            values = ",".join(self.bound(item) for item in ancestors)
            no_array = "NOT EXISTS" + self.template.format(
                expression="1", condition=f"p.path IN ({values}) AND p.value_type='array'"
            )
        missing = int(op == "exists" and not operand)
        return f"CASE WHEN {present} THEN {found} WHEN {scalar} OR {complete} OR (json_extract(o.metadata,'$.property_index.non_array_complete')=1 AND {no_array}) THEN {missing} ELSE NULL END"


def compile_filter(expression: dict[str, Any], kind: str) -> tuple[str, list[Any]]:
    """Compile nullable SQL evidence; every user literal is bound data."""
    validate_filter(expression)
    compiler = _Compiler(kind)
    return compiler.compile_node(expression), compiler.parameters


@dataclass(frozen=True)
class TextQuery:
    """A bounded literal query and safe FTS candidate expressions."""

    original: str
    mode: str
    tokens: tuple[str, ...]
    expressions: tuple[str, ...]
    token_start: int
    token_end: int


def compile_text_query(query: str, mode: str = "literal", *, tokenizer: apsw.FTS5Tokenizer | None = None) -> TextQuery:
    """Treat all user operators/quotes as data, never as FTS query syntax."""
    if mode not in ("literal", "words") or len(query.encode("utf-8")) > 2048:
        raise InvalidParamsError("invalid text query budget or mode")
    offsets = tokenize(query) if tokenizer is None else tokens(tokenizer, query.encode("utf-8"))
    if not offsets:
        raise InvalidParamsError("text query requires tokens")
    terms = tuple(item[2] for item in offsets)
    if mode == "words":
        terms = tuple(dict.fromkeys(terms))
    if len(terms) > 32:
        raise InvalidParamsError("text query token limit")
    quoted = tuple('"' + term.replace('"', '""') + '"' for term in terms)
    expressions = ('"' + " ".join(terms).replace('"', '""') + '"',) if mode == "literal" else quoted
    return TextQuery(query, mode, terms, expressions, offsets[0][0], offsets[-1][1])
