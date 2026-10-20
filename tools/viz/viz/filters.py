# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Translation of filter expressions into parameterized SQL.

Grammar:
    expr       := and_expr ("or" and_expr)*
    and_expr   := not_expr ("and" not_expr)*
    not_expr   := "not" not_expr | "(" expr ")" | comparison
    comparison := FIELD op operand | FIELD "is" ["not"] "null"
    op         := "==" | "=" | "!=" | "<" | "<=" | ">" | ">="
    operand    := NUMBER | STRING | HEX | WORD
A WORD naming a field compares against that field, any other WORD is a string.

RNTI fields, e.g. rnti or c_rnti, compare in the form of parsers.ran.rnti.normalize(), e.g. 0x0044, whether stored as
0x44, 0x0044 or 68: their values are normalized in SQL and their operands as parsers.ran.rnti.parse_any() reads them.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from parsers.ran import rnti as rnti_util

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<op>==|!=|<=|>=|=|<|>)"
    r"|(?P<paren>[()])"
    r"|(?P<string>\"[^\"]*\"|'[^']*')"
    r"|(?P<hex>0[xX][0-9a-fA-F]+)\b"
    r"|(?P<number>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\b"
    r"|(?P<word>[A-Za-z_][\w.#-]*)"
    r")"
)
_RNTI_FIELD_RE = re.compile(r".*rnti", re.IGNORECASE)
_KEYWORDS = {"and", "or", "not", "is", "null"}
_SQL_OPS = {"==": "=", "=": "=", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


class FilterError(ValueError):
    """Invalid filter expression."""


@dataclass
class _Token:
    kind: str
    text: str
    pos: int


def _tokenize(expr: str) -> list[_Token]:
    tokens = []
    pos = 0
    while pos < len(expr):
        if expr[pos:].strip() == "":
            break
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            raise FilterError(f"Unexpected character {expr[pos:].lstrip()[0]!r} at position {pos + 1}.")
        kind = m.lastgroup
        text = m.group(kind)
        start = m.start(kind)
        if kind == "word" and text.lower() in _KEYWORDS:
            kind, text = "keyword", text.lower()
        tokens.append(_Token(kind, text, start))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token], quote_field: Callable[[str], str], fields: dict[str, str]):
        self.tokens = tokens
        self.i = 0
        self.quote_field = quote_field
        self.fields = fields
        self.params: list[Any] = []

    def parse(self) -> str:
        sql = self.expr()
        if self.peek():
            self.fail("Unexpected", self.peek())
        return sql

    def peek(self) -> _Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self, what: str) -> _Token:
        tok = self.peek()
        if tok is None:
            raise FilterError(f"Expected {what} at the end of the filter.")
        self.i += 1
        return tok

    def accept_keyword(self, word: str) -> bool:
        tok = self.peek()
        if tok and tok.kind == "keyword" and tok.text == word:
            self.i += 1
            return True
        return False

    def fail(self, msg: str, tok: _Token) -> None:
        raise FilterError(f"{msg} {tok.text!r} at position {tok.pos + 1}.")

    def expr(self) -> str:
        parts = [self.and_expr()]
        while self.accept_keyword("or"):
            parts.append(self.and_expr())
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    def and_expr(self) -> str:
        parts = [self.not_expr()]
        while self.accept_keyword("and"):
            parts.append(self.not_expr())
        return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"

    def not_expr(self) -> str:
        if self.accept_keyword("not"):
            return f"(NOT {self.not_expr()})"
        tok = self.peek()
        if tok and tok.kind == "paren" and tok.text == "(":
            self.i += 1
            inner = self.expr()
            close = self.next("')'")
            if close.text != ")":
                self.fail("Expected ')' but found", close)
            return inner
        return self.comparison()

    def comparison(self) -> str:
        tok = self.next("a field name")
        if tok.kind != "word":
            self.fail("Expected a field name but found", tok)
        if tok.text not in self.fields:
            self.fail("Unknown field", tok)
        is_rnti = _is_rnti(tok.text)
        column = self.quote_field(tok.text)

        if self.accept_keyword("is"):
            negated = self.accept_keyword("not")
            null = self.next("'null'")
            if null.text != "null":
                self.fail("Expected 'null' but found", null)
            return f"({column} IS {'NOT ' if negated else ''}NULL)"

        op = self.next("a comparison operator")
        if op.kind != "op":
            self.fail("Expected a comparison operator but found", op)
        if is_rnti:
            column = _rnti_sql(column)
        return f"({column} {_SQL_OPS[op.text]} {self.operand(is_rnti)})"

    def operand(self, is_rnti: bool = False) -> str:
        tok = self.next("a value")
        if tok.kind == "word" and tok.text in self.fields:
            column = self.quote_field(tok.text)
            return _rnti_sql(column) if is_rnti and _is_rnti(tok.text) else column
        if is_rnti and tok.kind in ("number", "string", "hex", "word"):
            text = tok.text[1:-1] if tok.kind == "string" else tok.text
            # A value that is no RNTI compares as given, matching no normalized one.
            self.params.append(rnti_util.parse_any(text) or text)
        elif tok.kind == "number":
            self.params.append(float(tok.text) if any(c in tok.text for c in ".eE") else int(tok.text))
        elif tok.kind == "string":
            self.params.append(tok.text[1:-1])
        elif tok.kind == "hex":
            self.params.append(tok.text)
        elif tok.kind == "word":
            self.params.append(tok.text)
        else:
            self.fail("Expected a value but found", tok)
        return "?"


def _is_rnti(field: str) -> bool:
    return _RNTI_FIELD_RE.fullmatch(field) is not None


def _rnti_sql(column: str) -> str:
    """SQL of the value of an RNTI column as parsers.ran.rnti.normalize() gives it: integers, and 0x hex text of up to
    4 digits in any case, as 0x and 4 lowercase digits. Other values are left as they are.
    """
    low = f"lower({column})"
    return (
        f"(CASE WHEN typeof({column}) = 'integer' AND {column} BETWEEN 0 AND 65535 THEN printf('0x%04x', {column}) "
        f"WHEN typeof({column}) = 'text' AND length({column}) BETWEEN 3 AND 6 AND {low} GLOB '0x*' "
        f"AND NOT substr({low}, 3) GLOB '*[^0-9a-f]*' THEN '0x' || substr('000' || substr({low}, 3), -4) "
        f"ELSE {column} END)"
    )


def compile_filter(expr: str, fields: dict[str, str], quote_field: Callable[[str], str]) -> tuple[str, list[Any]]:
    """Translates a filter expression over the given fields into a SQL condition and its parameters."""
    tokens = _tokenize(expr)
    if not tokens:
        raise FilterError("Empty filter.")
    parser = _Parser(tokens, quote_field, fields)
    return parser.parse(), parser.params


def filter_rows(rows: Sequence[dict[str, Any]], expr: str, fields: Sequence[str]) -> list[dict[str, Any]]:
    """The rows matching a filter expression over the given fields, in order, evaluated by SQLite like the SQL of
    compile_filter(), so that both accept the same expressions.
    """
    cond, params = compile_filter(expr, dict.fromkeys(fields, ""), lambda f: '"' + f.replace('"', '""') + '"')
    columns = ", ".join('"' + f.replace('"', '""') + '"' for f in fields)
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(f"CREATE TABLE rows (_i INTEGER, {columns})")
        conn.executemany(
            f"INSERT INTO rows VALUES (?, {', '.join('?' * len(fields))})",
            [(i, *(row.get(f) for f in fields)) for i, row in enumerate(rows)],
        )
        kept = [i for (i,) in conn.execute(f"SELECT _i FROM rows WHERE {cond} ORDER BY _i", params)]
    return [rows[i] for i in kept]
