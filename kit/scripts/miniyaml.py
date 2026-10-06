"""A deliberately small YAML subset (the standard library has no YAML).

Supported: block mappings and block sequences nested by indentation (spaces
only), `- key: value` list-of-mapping items, the empty literals `[]` and `{}`,
comments, and scalars: true/false/null/~, ints, floats, 'single' and "double"
quoted strings, and plain strings. A document with no content loads as None.

Rejected with `line N`: anchors, aliases, tags, flow collections (other than
`[]`/`{}`), block scalars (| >), tab indentation, duplicate keys, multi-line
scalars. A block sequence may sit at the same indent as the key that holds it (the default
output of common YAML writers) or deeper; a leading UTF-8 byte-order mark is ignored.
"""
from __future__ import annotations

import re

from common import KitError


class YAMLError(KitError):
    pass


_INT = re.compile(r"[-+]?[0-9]+\Z")
_FLOAT = re.compile(r"[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+(?=[eE]))(?:[eE][-+]?[0-9]+)?\Z")
_ESCAPES = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0"}
_UNESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r", "\0": "\\0"}
_RESERVED_FIRST = {
    "&": "anchors are not supported", "*": "aliases are not supported", "!": "tags are not supported",
    "|": "block scalars are not supported", ">": "block scalars are not supported",
    "%": "directives are not supported", "@": "reserved indicator '@'", "`": "reserved indicator '`'",
}


def _err(line: int, message: str) -> YAMLError:
    return YAMLError(f"line {line}: {message}")


def _resolve_plain(text: str):
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~"):
        return None
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


def _read_quoted(text: str, line: int) -> tuple[str, str]:
    """Parse a quoted string at the start of `text`; return (value, remainder)."""
    quote = text[0]
    out: list[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if quote == "'" and ch == "'":
            if text[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), text[i + 1:]
        if quote == '"' and ch == '"':
            return "".join(out), text[i + 1:]
        if quote == '"' and ch == "\\":
            nxt = text[i + 1:i + 2]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[i + 2:i + 6]):
                out.append(chr(int(text[i + 2:i + 6], 16)))
                i += 6
                continue
            raise _err(line, f"unknown escape '\\{nxt}'")
        out.append(ch)
        i += 1
    raise _err(line, "unterminated quoted string")


def _after_quoted(rest: str, line: int) -> None:
    rest = rest.strip()
    if rest and not rest.startswith("#"):
        raise _err(line, "unexpected text after quoted string")


def _strip_comment(text: str) -> str:
    cut = text.find(" #")
    return (text if cut < 0 else text[:cut]).strip()


def _check_plain(text: str, line: int) -> None:
    if text[:1] in _RESERVED_FIRST:
        raise _err(line, _RESERVED_FIRST[text[0]])
    if text[0] in "[{" and text not in ("[]", "{}"):
        raise _err(line, "flow collections are not supported (only [] and {})")
    if text == "-" or text.startswith(("- ", "? ")) or text == "?":
        raise _err(line, "indicator not allowed here")
    if ": " in text or text.endswith(":"):
        raise _err(line, "mapping value not allowed here")


def _scalar(text: str, line: int):
    text = text.strip()
    if text[0] in "'\"":
        value, rest = _read_quoted(text, line)
        _after_quoted(rest, line)
        return value
    text = _strip_comment(text)
    _check_plain(text, line)
    if text == "[]":
        return []
    if text == "{}":
        return {}
    return _resolve_plain(text)


def _split_key(content: str, line: int):
    """Return (key, rest) if `content` is `key: ...`, else None."""
    if content[0] in "'\"":
        key, rest = _read_quoted(content, line)
        if rest[:1] == ":" and (len(rest) == 1 or rest[1] == " "):
            return key, rest[1:].strip()
        return None
    cut = content.find(" #")
    colon = content.find(": ")
    if colon < 0 and content.endswith(":"):
        colon = len(content) - 1
    if colon < 0 or (0 <= cut < colon):
        return None
    key = content[:colon].strip()
    if not key:
        raise _err(line, "empty key")
    _check_plain(key.replace(":", "_"), line)  # reserved first chars; ':' inside a key is fine
    return key, content[colon + 1:].strip()


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


class _Parser:
    def __init__(self, text: str):
        self.lines: list[tuple[int, int, str]] = []  # (line number, indent, content)
        for number, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
            body = raw.lstrip(" ")
            if not body.strip() or body.lstrip().startswith("#"):
                continue
            if body[0] == "\t":
                raise _err(number, "tab characters are not allowed in indentation")
            self.lines.append((number, len(raw) - len(body), body.rstrip()))

    def parse(self):
        if not self.lines:
            return None
        value, i = self.block(0, self.lines[0][1])
        if i < len(self.lines):
            raise _err(self.lines[i][0], "bad indentation")
        return value

    def block(self, i: int, indent: int):
        content = self.lines[i][2]
        if _is_item(content):
            return self.sequence(i, indent)
        if _split_key(content, self.lines[i][0]) is not None:
            return self.mapping(i, indent)
        return _scalar(content, self.lines[i][0]), i + 1

    def nested(self, i: int, indent: int):
        """Value of a `key:` or `-` with nothing after it: deeper block on the next line, else null."""
        if i < len(self.lines) and self.lines[i][1] > indent:
            return self.block(i, self.lines[i][1])
        return None, i

    def mapping(self, i: int, indent: int):
        result: dict = {}
        while i < len(self.lines):
            number, line_indent, content = self.lines[i]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise _err(number, "bad indentation")
            split = _split_key(content, number)
            if split is None:
                raise _err(number, "expected 'key: value'")
            key, rest = split
            if key in result:
                raise _err(number, f"duplicate key '{key}'")
            if rest and not rest.startswith("#"):
                result[key] = _scalar(rest, number)
                i += 1
            elif i + 1 < len(self.lines) and self.lines[i + 1][1] == indent and _is_item(self.lines[i + 1][2]):
                # `key:` then `- item` at the key's own indent: the list belongs to the key. (A list item's
                # own keys sit past its dash, so a dash at the dash column is the next item, not this value.)
                result[key], i = self.sequence(i + 1, indent, keyed=True)
            else:
                result[key], i = self.nested(i + 1, indent)
        return result, i

    def sequence(self, i: int, indent: int, keyed: bool = False):
        result: list = []
        while i < len(self.lines):
            number, line_indent, content = self.lines[i]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise _err(number, "bad indentation")
            if not _is_item(content):
                if keyed:
                    break  # the next key of the mapping that holds this list
                raise _err(number, "expected '- ' list item")
            rest = content[1:].lstrip(" ")
            if not rest or rest.startswith("#"):
                value, i = self.nested(i + 1, indent)
            else:
                item_indent = indent + (len(content) - len(rest))
                self.lines[i] = (number, item_indent, rest)  # treat the item body as its own line
                value, i = self.block(i, item_indent)
            result.append(value)
        return result, i


def loads(text: str):
    return _Parser(text[1:] if text.startswith("\ufeff") else text).parse()


def _needs_quotes(text: str) -> bool:
    return (text == "" or text != text.strip() or text[0] in "-?:,[]{}#&*!|>'\"%@`"
            or ": " in text or " #" in text or text.endswith(":")
            or any(ord(c) < 32 or ord(c) == 127 for c in text)
            or not isinstance(_resolve_plain(text), str))


def _quote(text: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        out = []
        for c in text:
            if c in _UNESCAPES:
                out.append(_UNESCAPES[c])
            elif ord(c) < 32 or ord(c) == 127:
                out.append(f"\\u{ord(c):04x}")
            else:
                out.append(c)
        return '"' + "".join(out) + '"'
    return "'" + text.replace("'", "''") + "'"


def _dump_key(key) -> str:
    if not isinstance(key, str):
        raise YAMLError(f"mapping keys must be strings, got {type(key).__name__}: {key!r}")
    return _quote(key) if _needs_quotes(key) else key


def _dump_scalar(value) -> str:
    if value is None:
        return "null"
    if value is True or value is False:
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise YAMLError(f"cannot represent {value!r}")
        return repr(value)  # always has a "." or exponent, so it re-parses as a float
    if isinstance(value, str):
        return _quote(value) if _needs_quotes(value) else value
    raise YAMLError(f"cannot dump {type(value).__name__}: {value!r}")


def _dump_block(value, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict) and value:
        lines: list[str] = []
        for key, item in value.items():
            name = _dump_key(key)
            if isinstance(item, (dict, list, tuple)) and item:
                lines.append(f"{pad}{name}:")
                lines.extend(_dump_block(item, indent + 2))
            else:
                lines.append(f"{pad}{name}: {_dump_inline(item)}")
        return lines
    if isinstance(value, (list, tuple)) and value:
        lines = []
        for item in value:
            if isinstance(item, (dict, list, tuple)) and item:
                inner = _dump_block(item, indent + 2)
                lines.append(f"{pad}- {inner[0][indent + 2:]}")
                lines.extend(inner[1:])
            else:
                lines.append(f"{pad}- {_dump_inline(item)}")
        return lines
    return [f"{pad}{_dump_inline(value)}"]


def _dump_inline(value) -> str:
    if isinstance(value, dict):
        if value:
            raise AssertionError("non-empty dict is not inline")
        return "{}"
    if isinstance(value, (list, tuple)):
        if value:
            raise AssertionError("non-empty list is not inline")
        return "[]"
    return _dump_scalar(value)


def dumps(data) -> str:
    return "\n".join(_dump_block(data, 0)) + "\n"
