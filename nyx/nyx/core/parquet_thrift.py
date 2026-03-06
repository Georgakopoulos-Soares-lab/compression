"""Minimal Thrift Compact Protocol serializer/deserializer for Parquet metadata.

Implements only the subset of Thrift Compact Protocol needed to read and write
Parquet file footers. No external dependencies.

Reference: https://github.com/apache/thrift/blob/master/doc/specs/thrift-compact-protocol.md
"""

from __future__ import annotations

import struct
from io import BytesIO

# Thrift Compact Protocol type IDs
T_STOP = 0
T_BOOLEAN_TRUE = 1
T_BOOLEAN_FALSE = 2
T_BYTE = 3
T_I16 = 4
T_I32 = 5
T_I64 = 6
T_DOUBLE = 7
T_BINARY = 8
T_LIST = 9
T_SET = 10
T_MAP = 11
T_STRUCT = 12

# Parquet enum values
PARQUET_TYPE_BOOLEAN = 0
PARQUET_TYPE_INT32 = 1
PARQUET_TYPE_INT64 = 2
PARQUET_TYPE_INT96 = 3
PARQUET_TYPE_FLOAT = 4
PARQUET_TYPE_DOUBLE = 5
PARQUET_TYPE_BYTE_ARRAY = 6
PARQUET_TYPE_FIXED_LEN_BYTE_ARRAY = 7

PARQUET_ENCODING_PLAIN = 0
PARQUET_PAGE_TYPE_DATA = 0

OPENZL_CODEC_ID = 100

# Parquet ConvertedType enum (field 6 in SchemaElement)
# Source: https://github.com/apache/parquet-format/blob/master/src/main/thrift/parquet.thrift
CONVERTED_TYPE_UTF8 = 0
CONVERTED_TYPE_DATE = 6
CONVERTED_TYPE_TIME_MILLIS = 7
CONVERTED_TYPE_TIME_MICROS = 8
CONVERTED_TYPE_TIMESTAMP_MILLIS = 9
CONVERTED_TYPE_TIMESTAMP_MICROS = 10
CONVERTED_TYPE_UINT_8 = 11
CONVERTED_TYPE_UINT_16 = 12
CONVERTED_TYPE_UINT_32 = 13
CONVERTED_TYPE_UINT_64 = 14
CONVERTED_TYPE_INT_8 = 15
CONVERTED_TYPE_INT_16 = 16
CONVERTED_TYPE_INT_32 = 17
CONVERTED_TYPE_INT_64 = 18


def _write_varint(buf: bytearray, n: int):
    n = n & 0xFFFFFFFFFFFFFFFF
    while n > 0x7F:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n & 0x7F)


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _write_field(buf: bytearray, field_id: int, type_id: int, last_field_id: int) -> int:
    delta = field_id - last_field_id
    if 0 < delta <= 15:
        buf.append((delta << 4) | type_id)
    else:
        buf.append(type_id)
        _write_varint(buf, _zigzag(field_id))
    return field_id


def _write_i32(buf: bytearray, field_id: int, value: int, last: int) -> int:
    last = _write_field(buf, field_id, T_I32, last)
    _write_varint(buf, _zigzag(value))
    return last


def _write_i64(buf: bytearray, field_id: int, value: int, last: int) -> int:
    last = _write_field(buf, field_id, T_I64, last)
    _write_varint(buf, _zigzag(value))
    return last


def _write_string(buf: bytearray, field_id: int, value: str, last: int) -> int:
    last = _write_field(buf, field_id, T_BINARY, last)
    encoded = value.encode("utf-8")
    _write_varint(buf, len(encoded))
    buf.extend(encoded)
    return last


def _write_binary(buf: bytearray, field_id: int, value: bytes, last: int) -> int:
    last = _write_field(buf, field_id, T_BINARY, last)
    _write_varint(buf, len(value))
    buf.extend(value)
    return last


def _write_bool(buf: bytearray, field_id: int, value: bool, last: int) -> int:
    type_id = T_BOOLEAN_TRUE if value else T_BOOLEAN_FALSE
    last = _write_field(buf, field_id, type_id, last)
    return last


def _write_list_header(buf: bytearray, field_id: int, elem_type: int, count: int, last: int) -> int:
    last = _write_field(buf, field_id, T_LIST, last)
    if count < 15:
        buf.append((count << 4) | elem_type)
    else:
        buf.append(0xF0 | elem_type)
        _write_varint(buf, count)
    return last


def _write_stop(buf: bytearray):
    buf.append(T_STOP)


# ======================================================================
# Parquet-specific structure serializers
# ======================================================================

ARROW_TO_PARQUET_TYPE = {
    "int8": PARQUET_TYPE_INT32,
    "int16": PARQUET_TYPE_INT32,
    "int32": PARQUET_TYPE_INT32,
    "int64": PARQUET_TYPE_INT64,
    "uint8": PARQUET_TYPE_INT32,
    "uint16": PARQUET_TYPE_INT32,
    "uint32": PARQUET_TYPE_INT32,
    "uint64": PARQUET_TYPE_INT64,
    "halffloat": PARQUET_TYPE_FLOAT,
    "float": PARQUET_TYPE_FLOAT,
    "float32": PARQUET_TYPE_FLOAT,
    "double": PARQUET_TYPE_DOUBLE,
    "float64": PARQUET_TYPE_DOUBLE,
    "bool": PARQUET_TYPE_BOOLEAN,
    "string": PARQUET_TYPE_BYTE_ARRAY,
    "large_string": PARQUET_TYPE_BYTE_ARRAY,
    "binary": PARQUET_TYPE_BYTE_ARRAY,
    "large_binary": PARQUET_TYPE_BYTE_ARRAY,
    "date32[day]": PARQUET_TYPE_INT32,
}


def _parquet_type_for_arrow(arrow_type_str: str) -> int:
    if arrow_type_str.startswith("timestamp"):
        return PARQUET_TYPE_INT64
    return ARROW_TO_PARQUET_TYPE.get(arrow_type_str, PARQUET_TYPE_BYTE_ARRAY)


def write_page_header(num_values: int, uncompressed_size: int, compressed_size: int) -> bytes:
    """Serialize a Parquet DataPageHeader (Thrift Compact Protocol)."""
    buf = bytearray()
    last = 0
    # PageHeader fields:
    # 1: PageType (i32) = DATA_PAGE (0)
    last = _write_i32(buf, 1, PARQUET_PAGE_TYPE_DATA, last)
    # 2: uncompressed_page_size (i32)
    last = _write_i32(buf, 2, uncompressed_size, last)
    # 3: compressed_page_size (i32)
    last = _write_i32(buf, 3, compressed_size, last)
    # 5: data_page_header (struct) — field 4 is crc, skip it
    last = _write_field(buf, 5, T_STRUCT, last)
    inner_last = 0
    # DataPageHeader.1: num_values (i32)
    inner_last = _write_i32(buf, 1, num_values, inner_last)
    # DataPageHeader.2: encoding (i32) = PLAIN (0)
    inner_last = _write_i32(buf, 2, PARQUET_ENCODING_PLAIN, inner_last)
    # DataPageHeader.3: definition_level_encoding (i32) = PLAIN (0)
    inner_last = _write_i32(buf, 3, PARQUET_ENCODING_PLAIN, inner_last)
    # DataPageHeader.4: repetition_level_encoding (i32) = PLAIN (0)
    inner_last = _write_i32(buf, 4, PARQUET_ENCODING_PLAIN, inner_last)
    _write_stop(buf)
    _write_stop(buf)  # end PageHeader
    return bytes(buf)


def write_statistics(
    min_val: bytes | None,
    max_val: bytes | None,
    null_count: int | None,
) -> bytes:
    """Serialize a Parquet Statistics struct.

    Field layout (from parquet.thrift):
        1: binary max          (deprecated, signed order)
        2: binary min          (deprecated, signed order)
        3: i64    null_count
        5: binary max_value    (correct column order)
        6: binary min_value    (correct column order)
    """
    buf = bytearray()
    last = 0
    if max_val is not None:
        last = _write_binary(buf, 1, max_val, last)
    if min_val is not None:
        last = _write_binary(buf, 2, min_val, last)
    if null_count is not None:
        last = _write_i64(buf, 3, null_count, last)
    if max_val is not None:
        last = _write_binary(buf, 5, max_val, last)
    if min_val is not None:
        last = _write_binary(buf, 6, min_val, last)
    _write_stop(buf)
    return bytes(buf)


def write_column_metadata(
    parquet_type: int,
    encodings: list[int],
    path_in_schema: list[str],
    codec: int,
    num_values: int,
    total_uncompressed: int,
    total_compressed: int,
    data_page_offset: int,
    statistics_bytes: bytes | None = None,
) -> bytes:
    """Serialize a ColumnMetaData struct."""
    buf = bytearray()
    last = 0
    # 1: type (i32)
    last = _write_i32(buf, 1, parquet_type, last)
    # 2: encodings (list<i32>)
    last = _write_list_header(buf, 2, T_I32, len(encodings), last)
    for enc in encodings:
        _write_varint(buf, _zigzag(enc))
    # 3: path_in_schema (list<string>)
    last = _write_list_header(buf, 3, T_BINARY, len(path_in_schema), last)
    for p in path_in_schema:
        encoded = p.encode("utf-8")
        _write_varint(buf, len(encoded))
        buf.extend(encoded)
    # 4: codec (i32)
    last = _write_i32(buf, 4, codec, last)
    # 5: num_values (i64)
    last = _write_i64(buf, 5, num_values, last)
    # 6: total_uncompressed_size (i64)
    last = _write_i64(buf, 6, total_uncompressed, last)
    # 7: total_compressed_size (i64)
    last = _write_i64(buf, 7, total_compressed, last)
    # 9: data_page_offset (i64) — field 8 is key_value_metadata, skip
    last = _write_i64(buf, 9, data_page_offset, last)
    # 12: statistics (optional struct) — fields 10,11 are index/dict page offsets
    if statistics_bytes is not None:
        last = _write_field(buf, 12, T_STRUCT, last)
        buf.extend(statistics_bytes)
    _write_stop(buf)
    return bytes(buf)


def write_column_chunk(file_offset: int, column_metadata_bytes: bytes) -> bytes:
    """Serialize a ColumnChunk struct."""
    buf = bytearray()
    last = 0
    # 2: file_offset (i64) — field 1 is file_path (unused for single-file)
    last = _write_i64(buf, 2, file_offset, last)
    # 3: meta_data (ColumnMetaData struct, inline)
    last = _write_field(buf, 3, T_STRUCT, last)
    buf.extend(column_metadata_bytes)
    _write_stop(buf)
    return bytes(buf)


def write_row_group(
    column_chunks_bytes: list[bytes],
    total_byte_size: int,
    num_rows: int,
) -> bytes:
    """Serialize a RowGroup struct."""
    buf = bytearray()
    last = 0
    # 1: columns (list<ColumnChunk>)
    last = _write_list_header(buf, 1, T_STRUCT, len(column_chunks_bytes), last)
    for cc in column_chunks_bytes:
        buf.extend(cc)
    # 2: total_byte_size (i64)
    last = _write_i64(buf, 2, total_byte_size, last)
    # 3: num_rows (i64)
    last = _write_i64(buf, 3, num_rows, last)
    _write_stop(buf)
    return bytes(buf)


def write_schema_element(
    name: str,
    num_children: int | None = None,
    parquet_type: int | None = None,
    repetition: int | None = None,
    converted_type: int | None = None,
) -> bytes:
    """Serialize a SchemaElement struct.

    repetition: 0=REQUIRED, 1=OPTIONAL, 2=REPEATED
    converted_type: Parquet ConvertedType enum value (e.g., UTF8=0 for strings)
    """
    buf = bytearray()
    last = 0
    if parquet_type is not None:
        last = _write_i32(buf, 1, parquet_type, last)
    if repetition is not None:
        last = _write_i32(buf, 3, repetition, last)
    last = _write_string(buf, 4, name, last)
    if num_children is not None:
        last = _write_i32(buf, 5, num_children, last)
    if converted_type is not None:
        last = _write_i32(buf, 6, converted_type, last)
    _write_stop(buf)
    return bytes(buf)


def write_key_value(key: str, value: str) -> bytes:
    """Serialize a KeyValue struct."""
    buf = bytearray()
    last = 0
    last = _write_string(buf, 1, key, last)
    last = _write_string(buf, 2, value, last)
    _write_stop(buf)
    return bytes(buf)


def write_file_metadata(
    version: int,
    schema_elements_bytes: list[bytes],
    row_groups_bytes: list[bytes],
    num_rows: int,
    key_value_pairs: list[tuple[str, str]] | None = None,
    created_by: str = "nyx (OpenZL)",
) -> bytes:
    """Serialize a FileMetaData struct (the Parquet footer)."""
    buf = bytearray()
    last = 0
    # 1: version (i32)
    last = _write_i32(buf, 1, version, last)
    # 2: schema (list<SchemaElement>)
    last = _write_list_header(buf, 2, T_STRUCT, len(schema_elements_bytes), last)
    for se in schema_elements_bytes:
        buf.extend(se)
    # 3: num_rows (i64)
    last = _write_i64(buf, 3, num_rows, last)
    # 4: row_groups (list<RowGroup>)
    last = _write_list_header(buf, 4, T_STRUCT, len(row_groups_bytes), last)
    for rg in row_groups_bytes:
        buf.extend(rg)
    # 5: key_value_metadata (list<KeyValue>)
    if key_value_pairs:
        last = _write_list_header(buf, 5, T_STRUCT, len(key_value_pairs), last)
        for k, v in key_value_pairs:
            buf.extend(write_key_value(k, v))
    # 6: created_by (string)
    if created_by:
        last = _write_string(buf, 6, created_by, last)
    _write_stop(buf)
    return bytes(buf)


# ======================================================================
# Thrift Compact Protocol reader (minimal, for footer parsing)
# ======================================================================

class ThriftReader:
    """Minimal Thrift Compact Protocol reader."""

    def __init__(self, data: bytes):
        self._buf = data
        self._pos = 0
        self._last_field_id = 0
        self._field_stack = []

    @property
    def pos(self) -> int:
        return self._pos

    def _read_byte(self) -> int:
        b = self._buf[self._pos]
        self._pos += 1
        return b

    def _read_varint(self) -> int:
        result = 0
        shift = 0
        while True:
            b = self._read_byte()
            result |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        return result

    def _read_zigzag(self) -> int:
        n = self._read_varint()
        return (n >> 1) ^ -(n & 1)

    def read_field_header(self) -> tuple[int, int]:
        """Returns (field_id, type_id). Returns (0, T_STOP) at end of struct."""
        b = self._read_byte()
        if b == T_STOP:
            return 0, T_STOP
        type_id = b & 0x0F
        delta = (b >> 4) & 0x0F
        if delta == 0:
            field_id = self._read_zigzag()
        else:
            field_id = self._last_field_id + delta
        self._last_field_id = field_id

        if type_id in (T_BOOLEAN_TRUE, T_BOOLEAN_FALSE):
            pass
        return field_id, type_id

    def read_i32(self) -> int:
        return self._read_zigzag()

    def read_i64(self) -> int:
        return self._read_zigzag()

    def read_binary(self) -> bytes:
        length = self._read_varint()
        data = self._buf[self._pos : self._pos + length]
        self._pos += length
        return data

    def read_string(self) -> str:
        return self.read_binary().decode("utf-8")

    def read_double(self) -> float:
        val = struct.unpack_from("<d", self._buf, self._pos)[0]
        self._pos += 8
        return val

    def read_list_header(self) -> tuple[int, int]:
        """Returns (count, element_type)."""
        b = self._read_byte()
        size = (b >> 4) & 0x0F
        elem_type = b & 0x0F
        if size == 0x0F:
            size = self._read_varint()
        return size, elem_type

    def push_struct(self):
        self._field_stack.append(self._last_field_id)
        self._last_field_id = 0

    def pop_struct(self):
        self._last_field_id = self._field_stack.pop()

    def skip(self, type_id: int):
        """Skip a value of the given type."""
        if type_id in (T_BOOLEAN_TRUE, T_BOOLEAN_FALSE):
            pass
        elif type_id == T_BYTE:
            self._pos += 1
        elif type_id in (T_I16, T_I32, T_I64):
            self._read_varint()
        elif type_id == T_DOUBLE:
            self._pos += 8
        elif type_id == T_BINARY:
            length = self._read_varint()
            self._pos += length
        elif type_id == T_LIST or type_id == T_SET:
            count, elem = self.read_list_header()
            for _ in range(count):
                self.skip(elem)
        elif type_id == T_MAP:
            count = self._read_varint()
            if count > 0:
                b = self._read_byte()
                kt = (b >> 4) & 0x0F
                vt = b & 0x0F
                for _ in range(count):
                    self.skip(kt)
                    self.skip(vt)
        elif type_id == T_STRUCT:
            self.push_struct()
            while True:
                fid, ftype = self.read_field_header()
                if ftype == T_STOP:
                    break
                self.skip(ftype)
            self.pop_struct()


def read_page_header(data: bytes) -> tuple[dict, int]:
    """Parse a Parquet PageHeader from bytes.

    Returns (header_dict, bytes_consumed).
    """
    r = ThriftReader(data)
    header = {}
    while True:
        fid, ftype = r.read_field_header()
        if ftype == T_STOP:
            break
        if fid == 1 and ftype == T_I32:
            header["page_type"] = r.read_i32()
        elif fid == 2 and ftype == T_I32:
            header["uncompressed_page_size"] = r.read_i32()
        elif fid == 3 and ftype == T_I32:
            header["compressed_page_size"] = r.read_i32()
        else:
            r.skip(ftype)
    return header, r.pos
