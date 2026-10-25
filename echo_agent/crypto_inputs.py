"""Bounded extraction of literal RSA parameters. Uploaded code is never run."""

from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from typing import Any

_NUMBER = re.compile(r"(?:0x[0-9a-fA-F]+|[0-9]+)\Z")
_PARAMETERS = ("n", "e", "c", "p", "q")
_DSA_PARAMETERS = ("p", "q", "g", "y")
_DSA_Y = re.compile(r"(?m)^[ \t]*y[ \t]*=[ \t]*([0-9]+)[ \t]*$")
_DEFAULT_REQUIRED = frozenset(("n", "e", "c"))
_MAX_SOURCE_NODES = 10000
_MAX_EXPR_NODES = 64
_MAX_BITS = 4096
_MAX_EXPONENT_BITS = 256
_COMMENT_DIGITS = re.compile(r"[0-9]+\Z")


def _integer(value: Any) -> int:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and len(value) <= 1234 and _NUMBER.fullmatch(value):
        number = int(value, 16 if value.startswith("0x") else 10)
    else:
        raise ValueError("RSA parameters must be literal decimal or hexadecimal integers")
    if number < 0 or number.bit_length() > _MAX_BITS:
        raise ValueError(f"RSA literal exceeds the {_MAX_BITS}-bit parameter limit")
    return number


def _bounded(number: int, label: str) -> int:
    if number < 0 or number.bit_length() > _MAX_BITS:
        raise ValueError(f"{label} exceeds the {_MAX_BITS}-bit expression limit")
    return number


def _eval_parameter_expr(node: ast.AST, values: dict[str, int], budget: list[int]) -> int:
    """Evaluate a tiny integer expression without eval/compile/imports."""
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("RSA parameter expression is too large")
    if isinstance(node, ast.Constant):
        if type(node.value) is not int:
            raise ValueError("RSA parameter expression only allows integer constants")
        return _integer(node.value)
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise ValueError(f"RSA parameter expression references unknown name {node.id}")
        return values[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        number = _eval_parameter_expr(node.operand, values, budget)
        return _bounded(number if isinstance(node.op, ast.UAdd) else -number, "RSA parameter expression")
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod)):
        left = _eval_parameter_expr(node.left, values, budget)
        right = _eval_parameter_expr(node.right, values, budget)
        if isinstance(node.op, (ast.FloorDiv, ast.Mod)) and right == 0:
            raise ValueError("RSA parameter expression divides by zero")
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.FloorDiv):
            result = left // right
        else:
            result = left % right
        return _bounded(result, "RSA parameter expression")
    raise ValueError(
        "RSA parameter expression supports only integers, known names, + - * // % and unary +/-")


def _pow_relation(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "pow" and len(node.args) == 3 and not node.keywords)


def _parse_python_source(input_id: str, text: str) -> tuple[list[tuple[str, Any, int | None]], dict[str, Any] | None]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError("unsupported RSA source: literal n/e/c assignments are required") from exc
    if sum(1 for _ in ast.walk(tree)) > _MAX_SOURCE_NODES:
        raise ValueError("RSA source exceeds the extraction node limit")
    literals: list[tuple[str, Any, int | None]] = []
    symbolic: dict[str, Any] | None = None
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
            if target not in _PARAMETERS:
                raise ValueError("RSA parameter files may only assign literal integers to n, e, c, p and q")
            if isinstance(value, ast.Constant) and type(value.value) is int:
                literals.append((target, value.value, node.lineno))
            elif target == "c" and _pow_relation(value):
                if symbolic is not None:
                    raise ValueError("RSA input may contain only one symbolic c = pow(...) relation")
                symbolic = {"input_id": input_id, "base": value.args[0], "exponent": value.args[1],
                            "modulus": value.args[2], "line": node.lineno}
            else:
                raise ValueError(
                    "RSA parameter files may only assign literal integers to n, e, c, p and q, "
                    "or one c = pow(plaintext, exponent, modulus) relation")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
            if target not in _PARAMETERS:
                raise ValueError("RSA parameter files may only declare n, e, c, p and q")
            if (node.value is None and isinstance(node.annotation, ast.Constant)
                    and type(node.annotation.value) is int):
                literals.append((target, node.annotation.value, node.lineno))
            elif (isinstance(node.annotation, ast.Name) and node.annotation.id == "int"
                  and isinstance(node.value, ast.Constant) and type(node.value.value) is int):
                literals.append((target, node.value.value, node.lineno))
            else:
                raise ValueError("RSA parameter annotations must be int-valued, e.g. e:103629 or n: int = 55")
        else:
            raise ValueError(
                "RSA independent verification accepts parameter-only files; "
                "dynamic Python programs are unsupported")
    writes = {name: 0 for name in _PARAMETERS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in writes:
            writes[node.id] += 1
    defined = {entry[0] for entry in literals}
    if symbolic is not None:
        defined.add("c")
    for name in _PARAMETERS:
        if writes[name] > 1:
            raise ValueError(f"RSA parameter {name} must have one literal top-level assignment")
        if writes[name] and name not in defined:
            raise ValueError(f"RSA parameter {name} must have one literal top-level assignment")
    return literals, symbolic


def _comment_ciphertext(text: str) -> int:
    values: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                body = token.string[1:].strip()
                if _COMMENT_DIGITS.fullmatch(body):
                    values.append(body)
    except (tokenize.TokenError, IndentationError) as exc:
        raise ValueError("unsupported RSA source: cannot tokenize comments") from exc
    if len(values) != 1:
        raise ValueError("symbolic RSA ciphertext needs exactly one decimal-only comment line")
    return _integer(values[0])


def extract_rsa(inputs: list[tuple[str, bytes]], required: set[str] | None = None) -> tuple[bytes, list[dict[str, Any]]]:
    required = required or _DEFAULT_REQUIRED
    values: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    source_input: dict[str, str] = {}
    symbolic_relation: tuple[dict[str, Any], int] | None = None

    for input_id, raw in inputs:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("unsupported RSA input: use n/e/c text, literal Python assignments or RSA JSON") from exc
        entries: list[tuple[str, Any, int | None, str]] = []
        has_symbolic = False
        if text.lstrip().startswith("{"):
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate RSA JSON key")
                    result[key] = value
                return result
            data = json.loads(text, object_pairs_hook=unique_object)
            if not isinstance(data, dict) or data.get("algorithm", "rsa") != "rsa":
                raise ValueError("unsupported RSA JSON input")
            entries = [(key, value, None, "rsa-json") for key, value in data.items() if key in _PARAMETERS]
        else:
            literals, symbolic = _parse_python_source(input_id, text)
            entries = [(name, value, line, "literal-assignments") for name, value, line in literals]
            if symbolic is not None:
                if symbolic_relation is not None:
                    raise ValueError("RSA input may contain only one symbolic c = pow(...) relation")
                symbolic_relation = (symbolic, _comment_ciphertext(text))
                has_symbolic = True
        if not entries and not has_symbolic:
            raise ValueError(f"input {input_id} contains no supported literal RSA n/e/c/p/q parameters")
        for name, value, line, input_format in entries:
            number = _integer(value)
            if name in values and values[name] != number:
                raise ValueError(f"conflicting RSA parameter {name} across inputs")
            values[name] = number
            source_input[name] = input_id
            sources.append({"input_id": input_id, "parameter": name, "line": line, "format": input_format})

    if ("p" in values) != ("q" in values):
        raise ValueError("RSA parameters p and q must be supplied together")
    if "p" in values:
        p, q = values["p"], values["q"]
        if p <= 1 or q <= 1 or p == q:
            raise ValueError("invalid RSA factors p and q")
        if "n" in values and values["n"] != p * q:
            raise ValueError("conflicting RSA parameter n with p*q")
        if "n" not in values:
            values["n"] = _bounded(p * q, "RSA modulus")
            sources.append({"input_id": source_input.get("p"), "parameter": "n", "line": None,
                            "format": "derived-pq"})

    if symbolic_relation is not None:
        symbolic, ciphertext = symbolic_relation
        base = symbolic["base"]
        if not isinstance(base, ast.Name) or base.id in _PARAMETERS or base.id in values:
            raise ValueError("RSA pow relation must use a single unknown plaintext name as its base")
        budget = [_MAX_EXPR_NODES]
        exponent = _eval_parameter_expr(symbolic["exponent"], values, budget)
        modulus = _eval_parameter_expr(symbolic["modulus"], values, budget)
        if exponent <= 1 or exponent.bit_length() > _MAX_EXPONENT_BITS:
            raise ValueError("invalid RSA exponent in pow relation")
        if modulus <= 1 or modulus.bit_length() > _MAX_BITS:
            raise ValueError("invalid RSA modulus in pow relation")
        if "e" in values and values["e"] != exponent:
            raise ValueError("RSA pow exponent does not match e")
        values.setdefault("e", exponent)
        if "n" in values:
            if values["n"] != modulus:
                raise ValueError("RSA pow modulus does not match n")
        else:
            values["n"] = modulus
            sources.append({"input_id": symbolic["input_id"], "parameter": "n", "line": None,
                            "format": "symbolic-pow"})
        if "c" in values:
            raise ValueError("RSA ciphertext is defined both literally and symbolically")
        values["c"] = ciphertext
        sources.append({"input_id": symbolic["input_id"], "parameter": "c", "line": symbolic["line"],
                        "format": "symbolic-pow"})

    if not required.issubset(values):
        raise ValueError("RSA instance data incomplete: required literal parameters are " + ",".join(sorted(required)))
    return "".join(f"{name}={values[name]}\n" for name in _PARAMETERS if name in values).encode("ascii"), sources


def _dsa_y_value(text: str) -> int | None:
    matches = _DSA_Y.findall(text)
    return _integer(matches[-1]) if matches else None


def extract_dsa(inputs: list[tuple[str, bytes]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Bounded static extraction of DSA public parameters. Uploaded code is never run."""
    values: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    for input_id, raw in inputs:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("unsupported DSA input: use a DSA JSON manifest or Python parameters") from exc
        if text.lstrip().startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("unsupported DSA JSON input") from exc
            if not isinstance(data, dict) or data.get("algorithm") not in {"dsa", "dsa-signature"}:
                raise ValueError("unsupported DSA JSON input")
            for key in _DSA_PARAMETERS + ("r1", "s1", "r2", "s2"):
                if key in data:
                    values[key] = _integer(data[key])
                    sources.append({"input_id": input_id, "parameter": key, "line": None,
                                    "format": "dsa-json"})
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ValueError("unsupported DSA source: parseable Python parameters are required") from exc
        if sum(1 for _ in ast.walk(tree)) > _MAX_SOURCE_NODES:
            raise ValueError("DSA source exceeds the extraction node limit")
        dynamic_y = False
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target, value = node.target.id, node.value
                if value is None and isinstance(node.annotation, ast.Constant):
                    value = node.annotation
            else:
                continue
            if target not in _DSA_PARAMETERS:
                continue
            if isinstance(value, ast.Constant) and type(value.value) is int:
                values[target] = _integer(value.value)
                sources.append({"input_id": input_id, "parameter": target, "line": node.lineno,
                                "format": "dsa-literal"})
            elif target == "y":
                dynamic_y = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                y = _dsa_y_value(node.value)
                if y is not None and "y" not in values:
                    values["y"] = y
                    sources.append({"input_id": input_id, "parameter": "y", "line": getattr(node, "lineno", None),
                                    "format": "dsa-comment"})
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.COMMENT:
                    y = _dsa_y_value(token.string[1:])
                    if y is not None and "y" not in values:
                        values["y"] = y
                        sources.append({"input_id": input_id, "parameter": "y", "line": token.start[0],
                                        "format": "dsa-comment"})
        except (tokenize.TokenError, IndentationError):
            pass
        if dynamic_y and "y" not in values:
            raise ValueError("DSA source needs y as an integer literal or a y = <integer> comment")
    required = {"p", "q", "g", "y"}
    if not required.issubset(values):
        raise ValueError("DSA instance data incomplete: required parameters are p,q,g,y")
    for name in required:
        if values[name] <= 1:
            raise ValueError(f"invalid DSA parameter {name}")
    if values["q"] >= values["p"] or (values["p"] - 1) % values["q"]:
        raise ValueError("invalid DSA parameters: q must divide p-1")
    return {name: values[name] for name in _DSA_PARAMETERS}, sources


_RSA_AES_NAMES = ("N", "c", "e", "iv", "ciphertext")
_RSA_AES_LINE = re.compile(r"^[ \t]*(N|c|e|iv|ciphertext)[ \t]*=[ \t]*(.+?)[ \t]*$")
_HEX_BYTES = re.compile(r"(?:[0-9a-fA-F]{2})+\Z")


def _rsa_aes_line_values(text: str) -> list[tuple[str, Any, int, str]]:
    """Extract literal ``NAME = value`` lines from a trusted original input.

    The file is never executed or imported: only single-line literal assignments in the
    whitelist are read, so a program that also contains imports/calls is still usable for
    the constants it declares (for example ``e = 3``).
    """
    entries: list[tuple[str, Any, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = _RSA_AES_LINE.match(line)
        if match is None:
            continue
        name, raw = match.group(1), match.group(2)
        if name in {"N", "c", "e"}:
            if not _NUMBER.fullmatch(raw):
                continue
            value: Any = _integer(raw)
        elif name == "iv":
            try:
                parsed = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                continue
            if isinstance(parsed, str) and _HEX_BYTES.fullmatch(parsed):
                parsed = bytes.fromhex(parsed)
            if not isinstance(parsed, bytes):
                continue
            value = parsed
        else:
            if raw[:2].lower() in {"b'", 'b"'}:
                try:
                    parsed = ast.literal_eval(raw)
                except (ValueError, SyntaxError):
                    continue
                if not isinstance(parsed, bytes):
                    continue
                value = parsed
            elif _HEX_BYTES.fullmatch(raw):
                value = bytes.fromhex(raw)
            else:
                continue
        entries.append((name, value, lineno, "key-value"))
    return entries


def looks_like_rsa_aes(inputs: list[tuple[str, bytes]]) -> bool:
    """True when the original inputs declare RSA parameters plus an AES-CBC ciphertext."""
    names: set[str] = set()
    for _, raw in inputs:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        names.update(name for name, *_ in _rsa_aes_line_values(text))
    return {"N", "c", "iv", "ciphertext"}.issubset(names)


def extract_rsa_aes(inputs: list[tuple[str, bytes]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse N/c/e/iv/ciphertext from trusted original inputs without executing them."""
    values: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    for input_id, raw in inputs:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("rsa-aes input must be UTF-8 text") from exc
        for name, value, line, input_format in _rsa_aes_line_values(text):
            if name in values and values[name] != value:
                raise ValueError(f"conflicting rsa-aes parameter {name} across inputs")
            values[name] = value
            sources.append({"input_id": input_id, "parameter": name, "line": line,
                            "format": input_format})
    missing = [name for name in _RSA_AES_NAMES if name not in values]
    if missing:
        raise ValueError("rsa-aes input needs N, c, e, iv and ciphertext; missing "
                         + ", ".join(missing))
    if values["N"].bit_length() > _MAX_BITS:
        raise ValueError(f"rsa-aes modulus exceeds the {_MAX_BITS}-bit limit")
    if values["e"].bit_length() > _MAX_EXPONENT_BITS:
        raise ValueError(f"rsa-aes exponent exceeds the {_MAX_EXPONENT_BITS}-bit limit")
    if len(values["iv"]) != 16:
        raise ValueError("rsa-aes iv must be 16 bytes")
    if not values["ciphertext"] or len(values["ciphertext"]) % 16:
        raise ValueError("rsa-aes ciphertext must be a non-empty multiple of 16 bytes")
    return values, sources
