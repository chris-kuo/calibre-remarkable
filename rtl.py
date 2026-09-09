#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Right-to-left page progression support.

EPUB declares right-to-left page flipping with

    <spine page-progression-direction="rtl">

in its OPF.  Calibre's PDF output ignores that attribute entirely (it is only
wired into the EPUB and MOBI output plugins), so this module adds it back on
top of the PDF that Plumber produces.

Two things happen for an RTL book:

1.  ``/ViewerPreferences << /Direction /R2L >>`` is written into the document
    catalog.  This is the standards-blessed way to say "this book flips
    right-to-left" and is honoured by Acrobat, Foxit, Sumatra and pdf.js.

2.  The physical page order is reversed.  Most e-ink readers - the reMarkable
    included - ignore /Direction, so reversing the pages is what actually makes
    the book behave like a Japanese or Arabic volume: you tap the *left* side of
    the screen to advance through the story.  The front cover ends up as the
    final PDF page, which is the correct "front" for an RTL book; the
    conversion step puts a second copy of the cover at the other end so that
    page 1 still thumbnails as the cover.

3.  ``/OpenAction`` is pointed at that final page, so a viewer that honours it
    opens the book on its cover rather than on the last page of the story.
    (The reMarkable ignores this too - the plugin additionally writes
    ``lastOpenedPage`` into the document's .metadata, which the device does
    honour.)

The page reversal is done by reversing the ``/Kids`` array of every node in the
page tree rather than by copying pages around, so page objects, links and the
outline (table of contents) are left completely untouched.  Both inline and
indirect ``/Kids`` arrays are handled - calibre's PDF output writes an indirect
one.  Everything is written as a PDF incremental update appended to the end of
the file, so the original bytes are never modified.
"""

import re
import struct
import zipfile

OPF_NS = 'http://www.idpf.org/2007/opf'


# --------------------------------------------------------------------------
# EPUB side: read page-progression-direction out of the OPF
# --------------------------------------------------------------------------

def detect_epub_page_progression(epub_path):
    """
    Return the EPUB's declared page progression direction.

    Args:
        epub_path: Path to an EPUB file

    Returns:
        'rtl', 'ltr', 'default', or None when the EPUB does not declare one
        (or cannot be read).
    """
    try:
        with zipfile.ZipFile(epub_path) as zf:
            for opf_name in _opf_names(zf):
                try:
                    raw = zf.read(opf_name)
                except KeyError:
                    continue
                direction = _spine_direction(raw)
                if direction:
                    return direction
    except Exception:
        pass
    return None


def _opf_names(zf):
    """Yield candidate OPF paths inside an open EPUB zip, best guess first."""
    names = []
    try:
        container = zf.read('META-INF/container.xml')
        for match in re.finditer(rb'full-path\s*=\s*["\']([^"\']+)["\']', container):
            name = match.group(1).decode('utf-8', 'replace')
            if name not in names:
                names.append(name)
    except Exception:
        pass
    for name in zf.namelist():
        if name.lower().endswith('.opf') and name not in names:
            names.append(name)
    return names


def _spine_direction(raw):
    """Pull page-progression-direction off the <spine> element of an OPF."""
    try:
        from lxml import etree
        root = etree.fromstring(raw, parser=etree.XMLParser(recover=True))
        if root is None:
            return None
        for el in root.iter():
            tag = el.tag
            if not isinstance(tag, str):
                continue
            if tag.rpartition('}')[2] != 'spine':
                continue
            for key, value in el.attrib.items():
                if key == 'page-progression-direction' or key.endswith('}page-progression-direction'):
                    value = (value or '').strip().lower()
                    if value:
                        return value
            return None
    except Exception:
        # lxml unavailable or the OPF is too broken to parse - fall back to a
        # plain regex over the spine element.
        match = re.search(
            rb'<\s*(?:[\w.-]+:)?spine\b[^>]*page-progression-direction\s*=\s*["\']([^"\']+)["\']',
            raw, re.IGNORECASE)
        if match:
            return match.group(1).decode('ascii', 'replace').strip().lower()
    return None


def should_use_rtl(epub_path, page_direction='auto'):
    """
    Decide whether the PDF for *epub_path* should flip right-to-left.

    Args:
        epub_path: Path to the source EPUB (may be None for non-EPUB sources)
        page_direction: 'auto' (follow the EPUB's OPF), 'rtl' (always) or
            'ltr' (never)

    Returns:
        bool
    """
    if page_direction == 'rtl':
        return True
    if page_direction == 'ltr':
        return False
    if not epub_path:
        return False
    return detect_epub_page_progression(epub_path) == 'rtl'


# --------------------------------------------------------------------------
# PDF side: a very small incremental-update writer
# --------------------------------------------------------------------------

class PdfEditError(Exception):
    pass


_WS = b'\x00\t\n\x0c\r '
_DELIM = b'()<>[]{}/%'
_OBJ_RE = re.compile(rb'(\d+)\s+(\d+)\s+obj\b')
_REF_RE = re.compile(rb'(\d+)\s+(\d+)\s+R\b')
_SUBSECTION_RE = re.compile(rb'(\d+)\s+(\d+)')
_XREF_ENTRY_RE = re.compile(rb'\s*(\d{10})\s+(\d{5})\s+([nf])')
_TOKEN_RE = re.compile(rb'[^\s()<>\[\]{}/%]+')


def _skip_ws(data, i):
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if c in _WS:
            i += 1
        elif c == b'%':
            while i < n and data[i:i + 1] not in b'\r\n':
                i += 1
        else:
            break
    return i


def _scan_literal_string(data, i):
    depth = 0
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if c == b'\\':
            i += 2
            continue
        if c == b'(':
            depth += 1
        elif c == b')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise PdfEditError('unterminated literal string')


def _scan_hex_string(data, i):
    end = data.find(b'>', i)
    if end < 0:
        raise PdfEditError('unterminated hex string')
    return end + 1


def _scan_bracketed(data, i, opener, closer):
    depth = 0
    n = len(data)
    while i < n:
        two = data[i:i + 2]
        c = data[i:i + 1]
        if two == opener:
            depth += 1
            i += 2
            continue
        if two == closer:
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        if len(opener) == 1:
            if c == opener:
                depth += 1
                i += 1
                continue
            if c == closer:
                depth -= 1
                i += 1
                if depth == 0:
                    return i
                continue
        if two == b'<<':
            i = _scan_bracketed(data, i, b'<<', b'>>')
            continue
        if c == b'<':
            i = _scan_hex_string(data, i)
            continue
        if c == b'(':
            i = _scan_literal_string(data, i)
            continue
        if c == b'[':
            i = _scan_bracketed(data, i, b'[', b']')
            continue
        if c == b'%':
            i = _skip_ws(data, i)
            continue
        i += 1
    raise PdfEditError('unterminated %s' % opener)


def _scan_dict(data, i):
    return _scan_bracketed(data, i, b'<<', b'>>')


def _scan_array(data, i):
    return _scan_bracketed(data, i, b'[', b']')


def _scan_value(data, i):
    """Return the end offset of the PDF object starting at *i*."""
    i = _skip_ws(data, i)
    c = data[i:i + 1]
    if not c:
        raise PdfEditError('unexpected end of file')
    if data[i:i + 2] == b'<<':
        return _scan_dict(data, i)
    if c == b'<':
        return _scan_hex_string(data, i)
    if c == b'(':
        return _scan_literal_string(data, i)
    if c == b'[':
        return _scan_array(data, i)
    if c == b'/':
        j = i + 1
        n = len(data)
        while j < n:
            ch = data[j:j + 1]
            if ch in _WS or ch in _DELIM:
                break
            j += 1
        return j
    match = _REF_RE.match(data, i)
    if match:
        return match.end()
    match = _TOKEN_RE.match(data, i)
    if match:
        return match.end()
    raise PdfEditError('unparseable value at %d' % i)


def _dict_items(data, start, end):
    """Yield (key_bytes, value_start, value_end) for a dict spanning start:end."""
    i = start + 2
    limit = end - 2
    while True:
        i = _skip_ws(data, i)
        if i >= limit:
            return
        if data[i:i + 1] != b'/':
            raise PdfEditError('expected a name key at %d' % i)
        key_end = _scan_value(data, i)
        key = data[i:key_end]
        value_start = _skip_ws(data, key_end)
        value_end = _scan_value(data, value_start)
        yield key, value_start, value_end
        i = value_end


def _dict_get(data, start, end, key):
    """Return (value_start, value_end) for *key*, or None."""
    for k, vs, ve in _dict_items(data, start, end):
        if k == key:
            return vs, ve
    return None


def _as_ref(chunk):
    match = _REF_RE.match(chunk.strip())
    if match and match.end() == len(chunk.strip()):
        return int(match.group(1))
    return None


# --------------------------------------------------------------------------
# cross reference tables
# --------------------------------------------------------------------------

def _png_undo_predictor(raw, columns, colors=1, bpc=8):
    row_len = columns * colors * bpc // 8
    out = bytearray()
    prev = bytearray(row_len)
    pos = 0
    n = len(raw)
    while pos + 1 <= n - 1:
        tag = raw[pos]
        pos += 1
        row = bytearray(raw[pos:pos + row_len])
        if len(row) < row_len:
            row.extend(b'\x00' * (row_len - len(row)))
        pos += row_len
        if tag == 0:
            pass
        elif tag == 1:
            for i in range(1, row_len):
                row[i] = (row[i] + row[i - 1]) & 0xFF
        elif tag == 2:
            for i in range(row_len):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif tag == 3:
            for i in range(row_len):
                left = row[i - 1] if i else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif tag == 4:
            for i in range(row_len):
                a = row[i - 1] if i else 0
                b = prev[i]
                c = prev[i - 1] if i else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pred) & 0xFF
        else:
            raise PdfEditError('unsupported PNG predictor %d' % tag)
        out.extend(row)
        prev = row
    return bytes(out)


def _decode_xref_stream_data(data, dict_start, dict_end, raw):
    import zlib
    filt = _dict_get(data, dict_start, dict_end, b'/Filter')
    if filt:
        name = data[filt[0]:filt[1]].strip()
        if name in (b'/FlateDecode', b'[/FlateDecode]', b'[ /FlateDecode ]'):
            raw = zlib.decompress(raw)
        elif name:
            raise PdfEditError('unsupported xref stream filter %s' % name)
    parms = _dict_get(data, dict_start, dict_end, b'/DecodeParms')
    if parms:
        ps, pe = parms
        if data[ps:ps + 2] == b'<<':
            pred = _dict_get(data, ps, pe, b'/Predictor')
            predictor = int(data[pred[0]:pred[1]]) if pred else 1
            if predictor >= 10:
                cols = _dict_get(data, ps, pe, b'/Columns')
                columns = int(data[cols[0]:cols[1]]) if cols else 1
                raw = _png_undo_predictor(raw, columns)
            elif predictor != 1:
                raise PdfEditError('unsupported predictor %d' % predictor)
    return raw


class _Pdf(object):
    """Just enough PDF parsing to read the catalog and page tree."""

    def __init__(self, data):
        self.data = data
        self.xref = {}
        self.trailer = {}
        self.startxref = self._find_startxref()
        self.uses_xref_streams = False
        self._load_xref(self.startxref, set())
        if b'/Encrypt' in self.trailer:
            raise PdfEditError('encrypted PDFs are not supported')

    # -- loading ---------------------------------------------------------
    def _find_startxref(self):
        tail = self.data[-2048:]
        idx = tail.rfind(b'startxref')
        if idx < 0:
            raise PdfEditError('no startxref found')
        match = re.compile(rb'startxref\s+(\d+)').match(tail, idx)
        if not match:
            raise PdfEditError('malformed startxref')
        return int(match.group(1))

    def _load_xref(self, offset, seen):
        if offset in seen or offset <= 0 or offset >= len(self.data):
            return
        seen.add(offset)
        data = self.data
        i = _skip_ws(data, offset)
        if data[i:i + 4] == b'xref':
            trailer_start, trailer_end = self._read_classic_xref(i + 4)
        else:
            trailer_start, trailer_end = self._read_xref_stream(i)
            self.uses_xref_streams = True
        for key, vs, ve in _dict_items(data, trailer_start, trailer_end):
            self.trailer.setdefault(key, data[vs:ve])
        prev = _dict_get(data, trailer_start, trailer_end, b'/Prev')
        if prev:
            try:
                self._load_xref(int(data[prev[0]:prev[1]]), seen)
            except ValueError:
                pass

    def _read_classic_xref(self, i):
        data = self.data
        while True:
            i = _skip_ws(data, i)
            if data[i:i + 7] == b'trailer':
                start = _skip_ws(data, i + 7)
                return start, _scan_dict(data, start)
            match = _SUBSECTION_RE.match(data, i)
            if not match:
                raise PdfEditError('malformed xref table at %d' % i)
            first, count = int(match.group(1)), int(match.group(2))
            i = match.end()
            for k in range(count):
                entry = _XREF_ENTRY_RE.match(data, i)
                if not entry:
                    raise PdfEditError('malformed xref entry at %d' % i)
                i = entry.end()
                if entry.group(3) == b'n':
                    self.xref.setdefault(first + k, (1, int(entry.group(1)), int(entry.group(2))))

    def _read_xref_stream(self, i):
        data = self.data
        match = _OBJ_RE.match(data, i)
        if not match:
            raise PdfEditError('expected an xref stream at %d' % i)
        dict_start = _skip_ws(data, match.end())
        dict_end = _scan_dict(data, dict_start)
        raw = self._stream_bytes(dict_start, dict_end)
        raw = _decode_xref_stream_data(data, dict_start, dict_end, raw)

        widths = _dict_get(data, dict_start, dict_end, b'/W')
        if not widths:
            raise PdfEditError('xref stream without /W')
        w = [int(x) for x in re.findall(rb'\d+', data[widths[0]:widths[1]])]
        size_span = _dict_get(data, dict_start, dict_end, b'/Size')
        size = int(data[size_span[0]:size_span[1]]) if size_span else 0
        index_span = _dict_get(data, dict_start, dict_end, b'/Index')
        if index_span:
            nums = [int(x) for x in re.findall(rb'\d+', data[index_span[0]:index_span[1]])]
            index = list(zip(nums[0::2], nums[1::2]))
        else:
            index = [(0, size)]

        row_len = sum(w)
        pos = 0
        for first, count in index:
            for k in range(count):
                if pos + row_len > len(raw):
                    break
                fields = []
                for width in w:
                    value = 0
                    for _ in range(width):
                        value = (value << 8) | raw[pos]
                        pos += 1
                    fields.append(value)
                ftype = fields[0] if w[0] else 1
                if ftype in (1, 2):
                    self.xref.setdefault(first + k, (ftype, fields[1], fields[2] if len(fields) > 2 else 0))
        return dict_start, dict_end

    def _stream_bytes(self, dict_start, dict_end):
        data = self.data
        i = _skip_ws(data, dict_end)
        if data[i:i + 6] != b'stream':
            raise PdfEditError('expected stream keyword')
        i += 6
        if data[i:i + 2] == b'\r\n':
            i += 2
        elif data[i:i + 1] in (b'\n', b'\r'):
            i += 1
        length = _dict_get(data, dict_start, dict_end, b'/Length')
        if length:
            chunk = data[length[0]:length[1]].strip()
            ref = _as_ref(chunk)
            if ref is None:
                return data[i:i + int(chunk)]
            span = self.object_span(ref)
            if span:
                return data[i:i + int(data[span[0]:span[1]].strip())]
        end = data.find(b'endstream', i)
        if end < 0:
            raise PdfEditError('unterminated stream')
        return data[i:end]

    # -- objects ---------------------------------------------------------
    def generation(self, num):
        """Generation number of object *num* (0 unless the file says otherwise)."""
        entry = self.xref.get(num)
        if entry and entry[0] == 1:
            return entry[2]
        return 0

    def object_span(self, num):
        """Return (start, end) of the value of object *num*, or None."""
        entry = self.xref.get(num)
        if not entry:
            return None
        if entry[0] == 1:
            data = self.data
            offset = entry[1]
            match = _OBJ_RE.match(data, _skip_ws(data, offset))
            if not match:
                match = _OBJ_RE.search(data, max(0, offset - 64), min(len(data), offset + 512))
            if not match or int(match.group(1)) != num:
                return None
            start = _skip_ws(data, match.end())
            return start, _scan_value(data, start)
        return None  # object streams: readable but not needed for our targets

    def object_from_stream(self, num):
        """Return the raw bytes of object *num* when it lives in an ObjStm."""
        entry = self.xref.get(num)
        if not entry or entry[0] != 2:
            return None
        span = self.object_span(entry[1])
        if not span:
            return None
        dict_start, dict_end = span[0], span[1]
        raw = self._stream_bytes(dict_start, dict_end)
        raw = _decode_xref_stream_data(self.data, dict_start, dict_end, raw)
        n_span = _dict_get(self.data, dict_start, dict_end, b'/N')
        first_span = _dict_get(self.data, dict_start, dict_end, b'/First')
        if not n_span or not first_span:
            return None
        count = int(self.data[n_span[0]:n_span[1]])
        first = int(self.data[first_span[0]:first_span[1]])
        nums = [int(x) for x in re.findall(rb'\d+', raw[:first])][:count * 2]
        pairs = list(zip(nums[0::2], nums[1::2]))
        for idx, (onum, ooff) in enumerate(pairs):
            if onum != num:
                continue
            start = first + ooff
            end = _scan_value(raw, start)
            return raw[start:end]
        return None

    def object_bytes(self, num):
        span = self.object_span(num)
        if span:
            return self.data[span[0]:span[1]]
        return self.object_from_stream(num)


# --------------------------------------------------------------------------
# the actual edits
# --------------------------------------------------------------------------

def _with_viewer_preferences_r2l(pdf, catalog):
    """Return catalog bytes with /ViewerPreferences << /Direction /R2L >>."""
    if not catalog.startswith(b'<<'):
        raise PdfEditError('catalog is not a dictionary')
    existing = _dict_get(catalog, 0, len(catalog), b'/ViewerPreferences')
    if existing is None:
        return b'<< /ViewerPreferences << /Direction /R2L >> ' + catalog[2:], None

    vs, ve = existing
    value = catalog[vs:ve]
    ref = _as_ref(value)
    if ref is not None:
        prefs = pdf.object_bytes(ref)
        if prefs is None or not prefs.strip().startswith(b'<<'):
            raise PdfEditError('cannot read /ViewerPreferences')
        return catalog, (ref, _set_direction(prefs.strip()))
    return catalog[:vs] + _set_direction(value) + catalog[ve:], None


def _set_direction(prefs):
    existing = _dict_get(prefs, 0, len(prefs), b'/Direction')
    if existing is None:
        return b'<< /Direction /R2L ' + prefs[2:]
    return prefs[:existing[0]] + b'/R2L' + prefs[existing[1]:]


def _first_leaf(pdf, node_ref, depth=0):
    """
    Return the (number, generation) of the first page in the tree.

    After the tree is reversed this is the *last* page of the document, which
    for an RTL book is its front cover - the page a reader should land on.
    """
    if depth > 32:
        raise PdfEditError('page tree nested too deeply')
    node = pdf.object_bytes(node_ref)
    if node is None:
        raise PdfEditError('cannot read page tree node %d' % node_ref)
    node = node.strip()
    kids = _dict_get(node, 0, len(node), b'/Kids')
    if kids is None:
        return (b'%d' % node_ref, b'%d' % pdf.generation(node_ref))
    array = node[kids[0]:kids[1]].strip()
    array_ref = _as_ref(array)
    if array_ref is not None:
        array = (pdf.object_bytes(array_ref) or b'').strip()
    refs = _REF_RE.findall(array)
    if not refs:
        raise PdfEditError('empty /Kids array')
    return _first_leaf(pdf, int(refs[0][0]), depth + 1)


def _with_open_action(catalog, page):
    """Point the catalog's /OpenAction at *page*, a (number, generation) pair."""
    action = b'[ %s %s R /Fit ]' % page
    existing = _dict_get(catalog, 0, len(catalog), b'/OpenAction')
    if existing is None:
        return b'<< /OpenAction ' + action + b' ' + catalog[2:]
    return catalog[:existing[0]] + action + catalog[existing[1]:]


def _reverse_page_tree(pdf, node_ref, updates, depth=0):
    """
    Reverse the page order of the tree rooted at *node_ref*, recursively.

    Reversing the /Kids array of every node - not just the root - reverses the
    leaves globally, and it does so without touching the page objects
    themselves, so /Parent links, /Count values, links and the outline all stay
    valid whether the tree is flat or nested.  /Kids may be written inline or
    as an indirect reference to an array object; calibre's PDF output uses the
    latter, so both are handled.

    Newly written objects are collected in *updates* rather than applied here.
    """
    if depth > 32:
        raise PdfEditError('page tree nested too deeply')
    node = pdf.object_bytes(node_ref)
    if node is None:
        raise PdfEditError('cannot read page tree node %d' % node_ref)
    node = node.strip()
    kids = _dict_get(node, 0, len(node), b'/Kids')
    if kids is None:
        return  # a leaf /Page

    ks, ke = kids
    value = node[ks:ke].strip()
    array_ref = _as_ref(value)
    if array_ref is None:
        array = value
    else:
        array = pdf.object_bytes(array_ref)
        if array is None:
            raise PdfEditError('cannot read the /Kids array object')
        array = array.strip()
    if not array.startswith(b'['):
        raise PdfEditError('/Kids is not an array')

    refs = _REF_RE.findall(array)
    if len(refs) > 1:
        new_array = b'[ ' + b' '.join(
            b'%s %s R' % (n, g) for n, g in reversed(refs)) + b' ]'
        if array_ref is None:
            updates[node_ref] = node[:ks] + new_array + node[ke:]
        else:
            updates[array_ref] = new_array

    for num, _gen in refs:
        child_ref = int(num)
        child = pdf.object_bytes(child_ref)
        if child is None:
            continue
        child = child.strip()
        if child.startswith(b'<<') and _dict_get(child, 0, len(child), b'/Kids') is not None:
            _reverse_page_tree(pdf, child_ref, updates, depth + 1)


def _contiguous_runs(numbers):
    runs = []
    for num in sorted(numbers):
        if runs and num == runs[-1][-1] + 1:
            runs[-1].append(num)
        else:
            runs.append([num])
    return runs


def _append_update(pdf, updates):
    """Append an incremental update defining *updates* ({objnum: bytes})."""
    data = pdf.data
    out = bytearray(data)
    if not out.endswith(b'\n'):
        out += b'\n'

    root = pdf.trailer.get(b'/Root')
    if not root:
        raise PdfEditError('trailer has no /Root')
    try:
        size = int(pdf.trailer.get(b'/Size', b'0'))
    except ValueError:
        size = 0
    size = max(size, max(updates) + 1)

    # An overridden object keeps its own object *and* generation number, or
    # existing references to it stop resolving.
    generations = {num: pdf.generation(num) for num in updates}
    offsets = {}
    for num in sorted(updates):
        offsets[num] = len(out)
        out += b'%d %d obj\n' % (num, generations[num]) + updates[num] + b'\nendobj\n'

    xref_offset = len(out)
    if pdf.uses_xref_streams:
        stream_num = size
        size += 1
        offsets[stream_num] = xref_offset
        generations[stream_num] = 0
        payload = bytearray()
        index = []
        for run in _contiguous_runs(offsets):
            index.append((run[0], len(run)))
            for num in run:
                payload += struct.pack(b'>BIH', 1, offsets[num], generations[num])
        index_bytes = b' '.join(b'%d %d' % pair for pair in index)
        header = (b'%d 0 obj\n<< /Type /XRef /Size %d /Index [ %s ] /W [ 1 4 2 ] '
                  b'/Root %s /Prev %d /Length %d >>\nstream\n'
                  % (stream_num, size, index_bytes, root.strip(),
                     pdf.startxref, len(payload)))
        out += header + bytes(payload) + b'\nendstream\nendobj\n'
        out += b'startxref\n%d\n%%%%EOF\n' % xref_offset
    else:
        out += b'xref\n'
        for run in _contiguous_runs(offsets):
            out += b'%d %d\n' % (run[0], len(run))
            for num in run:
                out += b'%010d %05d n \n' % (offsets[num], generations[num])
        trailer = b'<< /Size %d /Root %s /Prev %d' % (size, root.strip(), pdf.startxref)
        doc_id = pdf.trailer.get(b'/ID')
        if doc_id:
            trailer += b' /ID ' + doc_id.strip()
        trailer += b' >>'
        out += b'trailer\n' + trailer + b'\nstartxref\n%d\n%%%%EOF\n' % xref_offset
    return bytes(out)


def count_pages(pdf_path):
    """
    Number of pages in *pdf_path*, read from the page tree.

    Counting ``/Type /Page`` occurrences in the raw bytes - the fallback
    remarkable.py used to end up on - overcounts, because calibre's PDF
    pipeline leaves orphaned page objects behind that are no longer referenced
    by any ``/Kids`` array. This walks the tree instead.

    Returns:
        int, or None when the PDF cannot be parsed.
    """
    try:
        with open(pdf_path, 'rb') as f:
            pdf = _Pdf(f.read())
        root_ref = _as_ref(pdf.trailer.get(b'/Root', b''))
        catalog = pdf.object_bytes(root_ref).strip()
        pages_span = _dict_get(catalog, 0, len(catalog), b'/Pages')
        pages_ref = _as_ref(catalog[pages_span[0]:pages_span[1]])
        node = pdf.object_bytes(pages_ref).strip()
        count = _dict_get(node, 0, len(node), b'/Count')
        if count is not None:
            value = int(node[count[0]:count[1]])
            if value > 0:
                return value
    except Exception:
        pass
    return None


def make_pdf_rtl(pdf_path, reverse_pages=True):
    """
    Mark *pdf_path* as a right-to-left book, in place.

    Writes /ViewerPreferences << /Direction /R2L >> into the catalog and, when
    *reverse_pages* is true, reverses the page order so readers that ignore
    /Direction (the reMarkable among them) still flip right-to-left.

    Returns:
        True when the file was modified, False when it was left untouched.

    Raises:
        PdfEditError when the PDF cannot be edited safely.
    """
    with open(pdf_path, 'rb') as f:
        data = f.read()

    pdf = _Pdf(data)

    root_ref = _as_ref(pdf.trailer.get(b'/Root', b''))
    if root_ref is None:
        raise PdfEditError('trailer has no direct /Root reference')
    catalog = pdf.object_bytes(root_ref)
    if catalog is None:
        raise PdfEditError('cannot read the document catalog')
    catalog = catalog.strip()

    updates = {}
    new_catalog, prefs_update = _with_viewer_preferences_r2l(pdf, catalog)
    if prefs_update:
        updates[prefs_update[0]] = prefs_update[1]

    if reverse_pages:
        pages_span = _dict_get(catalog, 0, len(catalog), b'/Pages')
        if pages_span is None:
            raise PdfEditError('catalog has no /Pages')
        pages_ref = _as_ref(catalog[pages_span[0]:pages_span[1]])
        if pages_ref is None:
            raise PdfEditError('/Pages is not an indirect reference')
        # Resolve this before reversing: the tree's first leaf becomes its last
        # page, and that is where an RTL book should open.
        cover = _first_leaf(pdf, pages_ref)
        _reverse_page_tree(pdf, pages_ref, updates)
        new_catalog = _with_open_action(new_catalog, cover)

    if new_catalog != catalog:
        updates[root_ref] = new_catalog

    if not updates:
        return False

    out = _append_update(pdf, updates)
    tmp = pdf_path + '.rtl.tmp'
    with open(tmp, 'wb') as f:
        f.write(out)
    import os
    os.replace(tmp, pdf_path)
    return True
