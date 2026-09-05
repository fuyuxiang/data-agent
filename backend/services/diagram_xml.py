"""draw.io XML validation, auto-fix, and diagram operations.

Ported from next-ai-draw-io's xml-validation.ts and diagram-operations.ts
with simplifications for the Python/ElementTree environment.

Key differences from the TS version:
- Uses xml.etree.ElementTree instead of DOMParser
- No browser DOM APIs (DOMParser, XMLSerializer, querySelectorAll)
- Simplified auto-fix pipeline (covers most common LLM errors)
- No multi-page mxfile support in operations (single page only)
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from defusedxml.ElementTree import fromstring as safe_fromstring

MAX_XML_SIZE = 1_000_000
STRUCTURAL_ATTRS = {"edge", "parent", "source", "target", "vertex", "connectable"}
VALID_ENTITIES = {"lt", "gt", "amp", "quot", "apos"}

ROOT_CELLS = '<mxCell id="0"/>\n<mxCell id="1" parent="0"/>'


def _parse_xml(value: str) -> ET.Element:
    """Parse untrusted diagram XML with DTD, entities and external refs disabled."""
    try:
        return safe_fromstring(
            value, forbid_dtd=True, forbid_entities=True, forbid_external=True,
        )
    except Exception as exc:
        # Normalize defusedxml's security exceptions to the parse error already
        # handled by the validation and editing call sites.
        raise ET.ParseError(str(exc)) from exc


# ── XML Validation ──────────────────────────────────────────────


def _check_duplicate_structural_attrs(xml: str) -> str | None:
    """Check for duplicate structural attributes (edge, parent, source, etc.)."""
    for tag_match in re.finditer(r"<[^>]+>", xml):
        tag = tag_match.group(0)
        attrs: dict[str, int] = {}
        for attr_match in re.finditer(r"\s([a-zA-Z_:][a-zA-Z0-9_:.-]*)\s*=", tag):
            name = attr_match.group(1)
            attrs[name] = attrs.get(name, 0) + 1
        duplicates = [n for n, c in attrs.items() if c > 1 and n in STRUCTURAL_ATTRS]
        if duplicates:
            return f"Duplicate structural attribute(s): {', '.join(duplicates)}"
    return None


def _check_duplicate_ids(xml: str) -> str | None:
    """Check for duplicate cell IDs. Ids 0 and 1 are allowed to repeat in multi-page docs."""
    ids: dict[str, int] = {}
    for m in re.finditer(r'\bid\s*=\s*["\']([^"\']+)["\']', xml, re.IGNORECASE):
        id_val = m.group(1)
        ids[id_val] = ids.get(id_val, 0) + 1
    duplicates = [(id_val, count) for id_val, count in ids.items()
                  if count > 1 and id_val not in {"0", "1"}]
    if duplicates:
        names = [f"'{id_val}' ({count}x)" for id_val, count in duplicates[:5]]
        return f"Found duplicate cell ID(s): {', '.join(names)}"
    return None


def _check_unescaped_lt_in_attrs(xml: str) -> str | None:
    """Check for unescaped < in quoted attribute values."""
    for m in re.finditer(r'=\s*"([^"]*)"', xml):
        value = m.group(1)
        if "<" in value and "&lt;" not in value:
            return "Unescaped < character in attribute values"
    return None


def _check_entity_references(xml: str) -> str | None:
    """Check for invalid entity references and unescaped ampersands."""
    # Remove comments first
    cleaned = re.sub(r"<!--[\s\S]*?-->", "", xml)
    # Check bare ampersands
    if re.search(r"&(?!(?:lt|gt|amp|quot|apos|#))[a-zA-Z]", cleaned):
        return "Found unescaped & character(s)"
    # Check invalid entity names
    for m in re.finditer(r"&([a-zA-Z][a-zA-Z0-9]*);", cleaned):
        if m.group(1) not in VALID_ENTITIES:
            return f"Invalid entity reference: &{m.group(1)};"
    return None


def _check_empty_ids(xml: str) -> str | None:
    """Check for mxCell elements with empty id attributes."""
    if re.search(r'<mxCell[^>]*\sid\s*=\s*["\']\s*["\'][^>]*>', xml):
        return "Found mxCell element(s) with empty id attribute"
    return None


def validate_mxcell_structure(xml: str) -> str | None:
    """Validate draw.io XML for common issues. Returns None if valid, error string if invalid."""
    if len(xml) > MAX_XML_SIZE:
        pass  # warn only, don't reject

    # 1. Try ElementTree parse
    try:
        _parse_xml(xml)
    except ET.ParseError as exc:
        return f"XML syntax error: {exc}. Likely unescaped special characters in attribute values."

    # 2. CDATA wrapper
    if re.match(r"\s*<!\[CDATA\[", xml):
        return "XML is wrapped in CDATA section"

    # 3. Duplicate structural attributes
    err = _check_duplicate_structural_attrs(xml)
    if err:
        return err

    # 4. Unescaped < in attributes
    err = _check_unescaped_lt_in_attrs(xml)
    if err:
        return err

    # 5. Duplicate IDs
    err = _check_duplicate_ids(xml)
    if err:
        return err

    # 6. Entity references
    err = _check_entity_references(xml)
    if err:
        return err

    # 7. Empty IDs
    err = _check_empty_ids(xml)
    if err:
        return err

    return None


# ── Auto-Fix ────────────────────────────────────────────────────


def auto_fix_xml(xml: str) -> tuple[str, list[str]]:
    """Attempt to fix common XML issues. Returns (fixed_xml, list_of_fixes)."""
    fixed = xml
    fixes: list[str] = []

    # 0. Fix JSON-escaped XML
    if re.search(r'=\\"', fixed):
        fixed = fixed.replace('\\"', '"').replace("\\n", "\n")
        fixes.append("Fixed JSON-escaped XML")

    # 1. Remove CDATA wrapper
    if re.match(r"\s*<!\[CDATA\[", fixed):
        fixed = re.sub(r"^\s*<!\[CDATA\[", "", fixed)
        fixed = re.sub(r"\]\]>\s*$", "", fixed)
        fixes.append("Removed CDATA wrapper")

    # 2. Remove garbage text before XML root
    xml_start = re.search(r"<(\?xml|mxGraphModel|mxfile)", fixed, re.IGNORECASE)
    if xml_start and xml_start.start() > 0:
        fixed = fixed[xml_start.start():]
        fixes.append("Removed text before XML root")

    # 3. Fix duplicate structural attributes (keep first occurrence)
    duplicate_fixes = 0

    def _deduplicate_structural_attrs(tag_match: re.Match[str]) -> str:
        nonlocal duplicate_fixes
        new_tag = tag_match.group(0)
        for attr in STRUCTURAL_ATTRS:
            pattern = re.compile(rf"\s{attr}\s*=\s*[\"\'][^\"\']*[\"\']", re.IGNORECASE)
            if len(pattern.findall(new_tag)) > 1:
                kept = 0

                def _keep_first(match: re.Match[str]) -> str:
                    nonlocal kept
                    kept += 1
                    return match.group(0) if kept == 1 else ""

                new_tag = pattern.sub(_keep_first, new_tag)
                duplicate_fixes += 1
        return new_tag

    fixed = re.sub(r"<[^>]+>", _deduplicate_structural_attrs, fixed)
    if duplicate_fixes:
        fixes.append(f"Removed {duplicate_fixes} duplicate structural attribute(s)")

    # 4. Fix unescaped & characters
    amp_pattern = re.compile(r"&(?!(?:lt|gt|amp|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)")
    if amp_pattern.search(fixed):
        fixed = amp_pattern.sub("&amp;", fixed)
        fixes.append("Escaped unescaped & characters")

    # 5. Fix double-escaped entities
    double_escapes = [
        (r"&ampquot;", "&quot;"),
        (r"&amplt;", "&lt;"),
        (r"&ampgt;", "&gt;"),
        (r"&ampapos;", "&apos;"),
        (r"&ampamp;", "&amp;"),
    ]
    for pattern, replacement in double_escapes:
        if re.search(pattern, fixed):
            fixed = re.sub(pattern, replacement, fixed)
            fixes.append(f"Fixed double-escaped entity {pattern}")

    # 6. Fix <Cell> to <mxCell>
    if re.search(r"</?Cell[\s>]", fixed, re.IGNORECASE):
        fixed = re.sub(r"<Cell(\s)", "<mxCell$1", fixed, flags=re.IGNORECASE)
        fixed = re.sub(r"</Cell>", "</mxCell>", fixed, flags=re.IGNORECASE)
        fixes.append("Fixed <Cell> tags to <mxCell>")

    # 7. Fix common closing tag typos
    typos = [
        (r"</mxElement>", "</mxCell>"),
        (r"</mxcell>", "</mxCell>"),
        (r"</mxgeometry>", "</mxGeometry>"),
        (r"</mxpoint>", "</mxPoint>"),
    ]
    for wrong, right in typos:
        if re.search(wrong, fixed):
            fixed = re.sub(wrong, right, fixed)
            fixes.append(f"Fixed closing tag typo {wrong} → {right}")

    # 8. Fix unescaped < > in attribute values
    has_unescaped = False
    for m in re.finditer(r'=\s*"([^"]*)"', fixed):
        if "<" in m.group(1) and "&lt;" not in m.group(1):
            has_unescaped = True
            break
    if has_unescaped:
        fixed = re.sub(
            r'=\s*"([^"]*)"',
            lambda m: f'="{m.group(1).replace("<", "&lt;").replace(">", "&gt;")}"',
            fixed,
        )
        fixes.append("Escaped <> characters in attribute values")

    # 9. Fix empty id attributes — generate new IDs
    counter = 0
    def _gen_id(m):
        nonlocal counter
        counter += 1
        return f'<mxCell{m.group(1)} id="cell_{counter}"{m.group(2)}>'
    fixed_new = re.sub(
        r'<mxCell([^>]*)\sid\s*=\s*["\']\s*["\']([^>]*)>',
        _gen_id,
        fixed,
    )
    if fixed_new != fixed:
        counter_changed = counter > 0
        fixed = fixed_new
        if counter_changed:
            fixes.append(f"Generated {counter} missing ID(s)")

    # 10. Remove trailing garbage after last XML tag
    last_tag = None
    for m in re.finditer(r"</[a-zA-Z][a-zA-Z0-9]*>|/>", fixed):
        last_tag = m.end()
    if last_tag and last_tag < len(fixed):
        trailing = fixed[last_tag:].strip()
        if trailing and not re.match(r"^(\s*</[^>]+>)*\s*$", trailing):
            fixed = fixed[:last_tag]
            fixes.append("Removed trailing garbage after last XML tag")

    return fixed, fixes


def validate_and_fix_xml(xml: str) -> dict[str, Any]:
    """Validate XML and auto-fix if invalid. Returns {valid, error, fixed, fixes}."""
    error = validate_mxcell_structure(xml)
    if not error:
        return {"valid": True, "error": None, "fixed": None, "fixes": []}

    fixed_xml, fixes = auto_fix_xml(xml)
    error2 = validate_mxcell_structure(fixed_xml)
    if not error2:
        return {"valid": True, "error": None, "fixed": fixed_xml, "fixes": fixes}

    return {"valid": False, "error": error2, "fixed": fixed_xml if fixes else None, "fixes": fixes}


def is_mxcell_xml_complete(xml: str | None) -> bool:
    """Check if mxCell XML is complete (not truncated)."""
    trimmed = (xml or "").strip()
    if not trimmed:
        return False

    last_self_close = trimmed.rfind("/>")
    last_mxcell_close = trimmed.rfind("</mxCell>")
    last_valid_end = max(last_self_close, last_mxcell_close)
    if last_valid_end == -1:
        return False

    end_offset = 9 if last_mxcell_close > last_self_close else 2
    suffix = trimmed[last_valid_end + end_offset:]
    return re.match(r"^(\s*</[^>]+>)*\s*$", suffix) is not None


# ── Wrap with mxfile ────────────────────────────────────────────


def wrap_with_mxfile(xml: str) -> str:
    """Wrap bare mxCell/mxGraphModel XML in a complete <mxfile> structure."""
    if "<mxfile>" in xml:
        return xml

    if "<mxGraphModel>" in xml:
        # Already has mxGraphModel wrapper
        # Strip trailing LLM wrapper tags
        content = _strip_trailing_wrappers(xml)
        # Remove existing root cells id="0"/"1" to prevent duplication
        content = re.sub(r'<mxCell id="0"[^>]*/?>', '', content)
        content = re.sub(r'<mxCell id="1"[^>]*/?>', '', content)
        return f'<mxfile><diagram name="Page-1" id="page-1">{content}</diagram></mxfile>'

    if "<root>" in xml:
        # Has root wrapper — strip it
        content = re.sub(r"<root>", "", xml)
        content = re.sub(r"</root>", "", content)
    else:
        content = xml

    content = _strip_trailing_wrappers(content)
    # Remove existing root cells
    content = re.sub(r'<mxCell id="0"[^>]*/?>', '', content)
    content = re.sub(r'<mxCell id="1"[^>]*/?>', '', content)

    return (
        '<mxfile><diagram name="Page-1" id="page-1">'
        "<mxGraphModel><root>"
        f"{ROOT_CELLS}\n{content}"
        "</root></mxGraphModel>"
        "</diagram></mxfile>"
    )


def _strip_trailing_wrappers(xml: str) -> str:
    """Remove trailing LLM-added wrapper closing tags after the last valid mxCell."""
    # Find last valid mxCell ending
    last_self = xml.rfind("/>")
    last_close = xml.rfind("</mxCell>")
    last_end = max(last_self, last_close)
    if last_end == -1:
        return xml
    # Keep content up to last valid ending plus any legitimate closing wrapper tags
    offset = 9 if last_close > last_self else 2
    after = xml[last_end + offset:]
    # If what follows is just closing tags like </root></mxGraphModel></diagram></mxfile>, keep it
    if re.match(r"^(\s*</[a-zA-Z][a-zA-Z0-9]*>\s*)*$", after):
        return xml
    # Otherwise truncate
    return xml[:last_end + offset]


# ── Diagram Operations (update/add/delete by cell ID) ───────────


def apply_diagram_operations(xml_content: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply ID-based operations (update/add/delete) to draw.io XML.

    Returns {"result": serialized_xml, "errors": [{"type", "cellId", "message"}]}.
    """
    errors: list[dict[str, str]] = []

    # Parse the XML
    try:
        # Handle the case where XML might have multiple root-level elements
        # by wrapping in a temp root
        if "<mxfile>" in xml_content:
            doc = _parse_xml(xml_content)
        else:
            # Wrap bare content
            wrapped = f"<_wrap>{xml_content}</_wrap>"
            doc = _parse_xml(wrapped)

        # Find the <root> element
        root_el = _find_root_element(doc)
        if root_el is None:
            return {"result": xml_content, "errors": [{"type": "update", "cellId": "", "message": "No <root> element found"}]}

    except ET.ParseError as exc:
        return {"result": xml_content, "errors": [{"type": "update", "cellId": "", "message": f"XML parse error: {exc}"}]}

    # Build cell map
    cell_map: dict[str, ET.Element] = {}
    for cell in root_el.findall("mxCell"):
        id_val = cell.get("id")
        if id_val:
            cell_map[id_val] = cell

    # Process each operation
    for op in operations:
        op_type = op.get("operation", "")
        cell_id = str(op.get("cell_id", ""))
        new_xml = op.get("new_xml")

        if op_type == "update":
            existing = cell_map.get(cell_id)
            if not existing:
                errors.append({"type": "update", "cellId": cell_id, "message": f"Cell id=\"{cell_id}\" not found"})
                continue
            if not new_xml:
                errors.append({"type": "update", "cellId": cell_id, "message": "new_xml required for update"})
                continue

            try:
                new_cell = _parse_xml(f"<_wrap>{new_xml}</_wrap>").find("mxCell")
                if new_cell is None:
                    errors.append({"type": "update", "cellId": cell_id, "message": "new_xml must contain mxCell"})
                    continue
                new_id = new_cell.get("id")
                if new_id != cell_id:
                    errors.append({"type": "update", "cellId": cell_id, "message": f"ID mismatch: expected \"{cell_id}\" got \"{new_id}\""})
                    continue
                # ElementTree does not expose parents, so construct a parent map.
                parent_map = {c: p for p in root_el.iter() for c in p}
                parent_el = parent_map.get(existing, root_el)
                idx = list(parent_el).index(existing)
                parent_el.remove(existing)
                parent_el.insert(idx, new_cell)
                cell_map[cell_id] = new_cell
            except ET.ParseError:
                errors.append({"type": "update", "cellId": cell_id, "message": "new_xml parse error"})

        elif op_type == "add":
            if cell_map.get(cell_id):
                errors.append({"type": "add", "cellId": cell_id, "message": f"Cell id=\"{cell_id}\" already exists"})
                continue
            if not new_xml:
                errors.append({"type": "add", "cellId": cell_id, "message": "new_xml required for add"})
                continue
            try:
                new_cell = _parse_xml(f"<_wrap>{new_xml}</_wrap>").find("mxCell")
                if new_cell is None:
                    errors.append({"type": "add", "cellId": cell_id, "message": "new_xml must contain mxCell"})
                    continue
                new_id = new_cell.get("id")
                if new_id != cell_id:
                    errors.append({"type": "add", "cellId": cell_id, "message": f"ID mismatch: expected \"{cell_id}\" got \"{new_id}\""})
                    continue
                root_el.append(new_cell)
                cell_map[cell_id] = new_cell
            except ET.ParseError:
                errors.append({"type": "add", "cellId": cell_id, "message": "new_xml parse error"})

        elif op_type == "delete":
            # Protect root cells
            if cell_id in {"0", "1"}:
                errors.append({"type": "delete", "cellId": cell_id, "message": f"Cannot delete root cell \"{cell_id}\""})
                continue
            existing = cell_map.get(cell_id)
            if not existing:
                continue  # may have been cascade-deleted

            # Cascade: collect descendants + edges referencing deleted cells
            to_delete = set()
            _collect_descendants(cell_id, cell_map, to_delete)
            # Find edges referencing any cell in to_delete
            for del_id in list(to_delete):
                for cid, cell in cell_map.items():
                    if cell.get("source") == del_id or cell.get("target") == del_id:
                        if cid not in {"0", "1"}:
                            _collect_descendants(cid, cell_map, to_delete)

            for del_id in to_delete:
                el = cell_map.get(del_id)
                if el is not None:
                    parent_map = {c: p for p in root_el.iter() for c in p}
                    parent_el = parent_map.get(el, root_el)
                    parent_el.remove(el)
                    cell_map.pop(del_id, None)

    # Serialize back
    result = _serialize_element(doc)
    # Remove our temp wrapper if we added one
    if "<_wrap>" in xml_content or "<mxfile>" not in xml_content:
        result = result.replace("<_wrap>", "").replace("</_wrap>", "")

    return {"result": result, "errors": errors}


def _find_root_element(doc: ET.Element) -> ET.Element | None:
    """Find the <root> element within an mxfile or mxGraphModel structure."""
    # Direct <root>
    if doc.tag == "root":
        return doc
    # <mxfile><diagram><mxGraphModel><root>
    if doc.tag == "mxfile":
        for diagram in doc.findall("diagram"):
            model = diagram.find("mxGraphModel")
            if model is not None:
                root = model.find("root")
                if root is not None:
                    return root
    # <mxGraphModel><root>
    if doc.tag == "mxGraphModel":
        return doc.find("root")
    # <_wrap> wrapper
    if doc.tag == "_wrap":
        for child in doc:
            root = _find_root_element(child)
            if root is not None:
                return root
    # Search deeper
    for root_el in doc.iter("root"):
        return root_el
    return None


def _collect_descendants(cell_id: str, cell_map: dict[str, ET.Element], collected: set) -> None:
    """Recursively collect cell_id and all its children."""
    if cell_id in collected:
        return
    collected.add(cell_id)
    for cid, cell in cell_map.items():
        if cell.get("parent") == cell_id and cid not in {"0", "1"}:
            _collect_descendants(cid, cell_map, collected)


def _serialize_element(element: ET.Element) -> str:
    """Serialize an ElementTree element back to XML string."""
    # Use short_empty_elements=True for <mxCell id="0"/> style
    return ET.tostring(element, encoding="unicode", short_empty_elements=True)
