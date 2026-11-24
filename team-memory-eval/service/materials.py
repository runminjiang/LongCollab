"""只公开原题环境列出的文件。可搜索的文本在宿主从原文件提取。"""

import base64
from xml.etree import ElementTree
from zipfile import ZipFile


def visible(task, scopes):
    return {name: p for name, (scope, p) in task.materials.items() if scope in scopes}


def searchable(path):
    suffix = path.suffix.lower()
    if suffix == ".docx":
        with ZipFile(path) as archive:
            doc = ElementTree.fromstring(archive.read("word/document.xml"))
            return "\n".join("".join(p.itertext()) for p in doc.findall(".//{*}p"))
    if suffix == ".xlsx":
        with ZipFile(path) as book:
            strings = []
            if "xl/sharedStrings.xml" in book.namelist():
                table = ElementTree.fromstring(book.read("xl/sharedStrings.xml"))
                strings = ["".join(i.itertext()) for i in table.findall("{*}si")]
            lines = []
            for name in sorted(
                n
                for n in book.namelist()
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            ):
                lines.append(name)
                for row in ElementTree.fromstring(book.read(name)).findall(".//{*}row"):
                    cells = []
                    for cell in row.findall("{*}c"):
                        value = cell.findtext("{*}v", "")
                        if cell.get("t") == "s" and value:
                            value = strings[int(value)]
                        elif cell.get("t") == "inlineStr":
                            value = "".join(cell.itertext())
                        if value:
                            cells.append(f"{cell.get('r', '')}={value}")
                    if cells:
                        lines.append(" | ".join(cells))
            return "\n".join(lines)
    return path.read_text(errors="replace")


def access(task, actor, scopes, op, args):
    files = visible(task, scopes)
    if op == "list":
        result = {
            "files": sorted(files),
            "operations": [n for n, (s, _) in task.operations.items() if s in scopes],
        }
        if files:
            result["read_files"] = (
                "These files live in the team service. In Bash use python -m tools.client read 'FILENAME' to print its text, or add a local destination to save the original."
            )
        return result
    if op == "read":
        path = files[args["name"]]
        if args.get("binary"):
            return {"base64": base64.b64encode(path.read_bytes()).decode()}
        return {"text": searchable(path)}
    if op == "search":
        if not files and task.operations:
            raise ValueError(
                "No material files in this task. Use python -m tools.client list and python -m tools.client call NAME JSON for its research tools."
            )
        query = args["query"].casefold()
        matches = []
        for name, path in files.items():
            text = searchable(path)
            idx = text.casefold().find(query)
            if idx >= 0:
                matches.append(
                    {"name": name, "excerpt": text[max(0, idx - 120) : idx + 280]}
                )
        return matches
    scope, handler = task.operations[op]
    if scope not in scopes:
        raise PermissionError("无权调用环境工具")
    return handler(actor, args)
