import ast
from typing import Optional, Tuple


def extract_interface(code: str) -> Tuple[str, Optional[str]]:
    try:
        tree = ast.parse(code)
    except Exception:
        return code, None

    entry_point: Optional[str] = None
    filtered_body = []

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef)):
            filtered_body.append(node)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if entry_point is None:
                entry_point = node.name

            docstring = ast.get_docstring(node)
            new_body = []
            if docstring:
                new_body.append(ast.Expr(value=ast.Constant(value=docstring)))
            new_body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))

            node.body = new_body
            filtered_body.append(node)

    tree.body = filtered_body

    ast.fix_missing_locations(tree)

    try:
        stub_code = ast.unparse(tree)
    except Exception:
        stub_code = code

    return stub_code, entry_point