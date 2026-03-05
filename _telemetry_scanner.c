/*
 * _telemetry_scanner.c — Fast C JSON scanner for telemetry JSONL codec.
 *
 * Replaces the Python _parse_object_raw / per-line parsing with C-speed
 * scanning.  Preserves exact numeric text (no float normalisation).
 *
 * Provides:
 *   parse_line(line)  -> (record_type, row_dict, top_keys) | None
 *   parse_object_raw(s) -> (dict, end_pos, keys)
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* Low-level JSON scanning helpers                                     */
/* ------------------------------------------------------------------ */

static inline Py_ssize_t skip_ws(const char *s, Py_ssize_t pos, Py_ssize_t len)
{
    while (pos < len) {
        char c = s[pos];
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n')
            break;
        pos++;
    }
    return pos;
}

/* Scan a JSON string starting at the opening '"'.
   Returns position after the closing '"'. */
static Py_ssize_t scan_string(const char *s, Py_ssize_t pos, Py_ssize_t len)
{
    pos++;  /* skip opening quote */
    while (pos < len) {
        char c = s[pos];
        if (c == '\\') {
            pos += 2;
        } else if (c == '"') {
            return pos + 1;
        } else {
            pos++;
        }
    }
    return pos;
}

/* Scan a bracketed value ({…} or […]).
   Returns position after the closing bracket. */
static Py_ssize_t scan_bracket(const char *s, Py_ssize_t pos, Py_ssize_t len,
                                char open_ch, char close_ch)
{
    int depth = 1;
    int in_string = 0;
    pos++;  /* skip opening bracket */
    while (pos < len && depth > 0) {
        char c = s[pos];
        if (in_string) {
            if (c == '\\')
                pos++;
            else if (c == '"')
                in_string = 0;
        } else {
            if (c == '"')
                in_string = 1;
            else if (c == open_ch)
                depth++;
            else if (c == close_ch)
                depth--;
        }
        pos++;
    }
    return pos;
}

/* Scan any JSON value.  Returns position after the value. */
static Py_ssize_t scan_value(const char *s, Py_ssize_t pos, Py_ssize_t len)
{
    pos = skip_ws(s, pos, len);
    if (pos >= len)
        return pos;

    char ch = s[pos];

    if (ch == '"')
        return scan_string(s, pos, len);
    if (ch == '{')
        return scan_bracket(s, pos, len, '{', '}');
    if (ch == '[')
        return scan_bracket(s, pos, len, '[', ']');

    /* true / false / null */
    if (len - pos >= 4 && memcmp(s + pos, "true", 4) == 0)
        return pos + 4;
    if (len - pos >= 5 && memcmp(s + pos, "false", 5) == 0)
        return pos + 5;
    if (len - pos >= 4 && memcmp(s + pos, "null", 4) == 0)
        return pos + 4;

    /* Number (or other literal) — scan until delimiter */
    while (pos < len) {
        char c = s[pos];
        if (c == ',' || c == '}' || c == ']' ||
            c == ' ' || c == '\t' || c == '\r' || c == '\n')
            break;
        pos++;
    }
    return pos;
}

/* ------------------------------------------------------------------ */
/* Parse a JSON object into (dict, keys_list).                         */
/* Returns 0 on success, -1 on error (Python exception set).          */
/* ------------------------------------------------------------------ */

static int parse_object(const char *s, Py_ssize_t pos, Py_ssize_t len,
                        PyObject **out_dict, PyObject **out_keys,
                        Py_ssize_t *out_pos)
{
    *out_dict = PyDict_New();
    *out_keys = PyList_New(0);
    if (!*out_dict || !*out_keys)
        return -1;

    pos = skip_ws(s, pos, len);
    if (pos >= len || s[pos] != '{') {
        *out_pos = pos;
        return 0;
    }
    pos++;  /* skip '{' */

    while (1) {
        pos = skip_ws(s, pos, len);
        if (pos >= len)
            break;
        if (s[pos] == '}') {
            pos++;
            break;
        }

        /* Key must be a string */
        if (s[pos] != '"') break;

        Py_ssize_t key_start = pos + 1;     /* after opening quote */
        Py_ssize_t key_end_pos = scan_string(s, pos, len);
        Py_ssize_t key_end = key_end_pos - 1;  /* before closing quote */

        /* Check for escape sequences in key */
        int has_escape = 0;
        for (Py_ssize_t i = key_start; i < key_end; i++) {
            if (s[i] == '\\') { has_escape = 1; break; }
        }

        PyObject *key_obj;
        if (has_escape) {
            /* Rare: use json.loads to decode the key */
            PyObject *key_raw = PyUnicode_FromStringAndSize(
                s + pos, key_end_pos - pos);
            if (!key_raw) return -1;

            PyObject *json_mod = PyImport_ImportModule("json");
            if (!json_mod) { Py_DECREF(key_raw); return -1; }
            PyObject *loads = PyObject_GetAttrString(json_mod, "loads");
            Py_DECREF(json_mod);
            if (!loads) { Py_DECREF(key_raw); return -1; }
            key_obj = PyObject_CallOneArg(loads, key_raw);
            Py_DECREF(loads);
            Py_DECREF(key_raw);
            if (!key_obj) return -1;
        } else {
            key_obj = PyUnicode_FromStringAndSize(
                s + key_start, key_end - key_start);
            if (!key_obj) return -1;
        }

        /* Skip ':' */
        pos = skip_ws(s, key_end_pos, len);
        if (pos < len && s[pos] == ':')
            pos++;

        /* Value — capture as raw text */
        pos = skip_ws(s, pos, len);
        Py_ssize_t val_start = pos;
        pos = scan_value(s, pos, len);

        PyObject *val_obj = PyUnicode_FromStringAndSize(
            s + val_start, pos - val_start);
        if (!val_obj) { Py_DECREF(key_obj); return -1; }

        PyDict_SetItem(*out_dict, key_obj, val_obj);
        PyList_Append(*out_keys, key_obj);
        Py_DECREF(key_obj);
        Py_DECREF(val_obj);

        /* Skip ',' */
        pos = skip_ws(s, pos, len);
        if (pos < len && s[pos] == ',')
            pos++;
    }

    *out_pos = pos;
    return 0;
}

/* ------------------------------------------------------------------ */
/* Envelope field detection                                            */
/* ------------------------------------------------------------------ */

static int is_envelope_field(const char *key, Py_ssize_t key_len)
{
    /* Fields: creator, current-time, host-name, key, node-id,
       server-start-time, type */
    switch (key_len) {
    case 3:
        return memcmp(key, "key", 3) == 0;
    case 4:
        return memcmp(key, "type", 4) == 0;
    case 7:
        return (memcmp(key, "creator", 7) == 0 ||
                memcmp(key, "node-id", 7) == 0);
    case 9:
        return memcmp(key, "host-name", 9) == 0;
    case 12:
        return memcmp(key, "current-time", 12) == 0;
    case 17:
        return memcmp(key, "server-start-time", 17) == 0;
    default:
        return 0;
    }
}

/* ------------------------------------------------------------------ */
/* Python-exposed: parse_line(line) -> (type, row_dict, top_keys)|None */
/* ------------------------------------------------------------------ */

static PyObject *py_parse_line(PyObject *self, PyObject *args)
{
    const char *line;
    Py_ssize_t line_len;

    if (!PyArg_ParseTuple(args, "s#", &line, &line_len))
        return NULL;

    /* Strip trailing whitespace */
    while (line_len > 0) {
        char c = line[line_len - 1];
        if (c != '\n' && c != '\r' && c != ' ' && c != '\t')
            break;
        line_len--;
    }

    if (line_len == 0)
        Py_RETURN_NONE;

    /* Parse top-level object */
    PyObject *top_dict = NULL, *top_keys = NULL;
    Py_ssize_t end_pos;
    if (parse_object(line, 0, line_len, &top_dict, &top_keys, &end_pos) < 0)
        goto error;

    if (PyDict_Size(top_dict) == 0) {
        Py_DECREF(top_dict);
        Py_DECREF(top_keys);
        Py_RETURN_NONE;
    }

    /* Extract record type */
    PyObject *type_key = PyUnicode_FromString("type");
    PyObject *type_val = PyDict_GetItem(top_dict, type_key);  /* borrowed ref */
    PyObject *record_type;

    if (type_val) {
        const char *tv = PyUnicode_AsUTF8(type_val);
        Py_ssize_t tv_len = PyUnicode_GET_LENGTH(type_val);
        if (tv_len >= 2 && tv[0] == '"') {
            record_type = PyUnicode_FromStringAndSize(tv + 1, tv_len - 2);
        } else {
            record_type = type_val;
            Py_INCREF(record_type);
        }
    } else {
        record_type = PyUnicode_FromString("__notype__");
    }
    Py_DECREF(type_key);

    /* Build row_dict: envelope fields + content fields + __ckeys__ */
    PyObject *row_dict = PyDict_New();
    if (!row_dict) goto error_rt;

    PyObject *content_val = NULL;  /* borrowed then INCREF'd */
    Py_ssize_t num_keys = PyList_GET_SIZE(top_keys);

    for (Py_ssize_t i = 0; i < num_keys; i++) {
        PyObject *k = PyList_GET_ITEM(top_keys, i);  /* borrowed */
        const char *ks = PyUnicode_AsUTF8(k);
        Py_ssize_t klen = PyUnicode_GET_LENGTH(k);

        /* Skip type and content */
        if (klen == 4 && memcmp(ks, "type", 4) == 0)
            continue;
        if (klen == 7 && memcmp(ks, "content", 7) == 0) {
            content_val = PyDict_GetItem(top_dict, k);  /* borrowed */
            Py_XINCREF(content_val);
            continue;
        }

        PyObject *val = PyDict_GetItem(top_dict, k);  /* borrowed */

        if (is_envelope_field(ks, klen)) {
            PyDict_SetItem(row_dict, k, val);
        } else {
            /* Prefix with "e." */
            PyObject *prefixed = PyUnicode_FromFormat("e.%s", ks);
            if (prefixed) {
                PyDict_SetItem(row_dict, prefixed, val);
                Py_DECREF(prefixed);
            }
        }
    }

    /* Process content field */
    PyObject *ckeys_str;
    if (content_val) {
        const char *cv = PyUnicode_AsUTF8(content_val);
        if (cv && cv[0] == '{') {
            Py_ssize_t cv_len = PyUnicode_GET_LENGTH(content_val);
            PyObject *content_dict = NULL, *content_keys = NULL;
            Py_ssize_t cend;
            if (parse_object(cv, 0, cv_len, &content_dict, &content_keys,
                             &cend) < 0) {
                Py_DECREF(content_val);
                goto error_rd;
            }

            /* Build comma-separated content keys and add c.xxx fields */
            Py_ssize_t num_ckeys = PyList_GET_SIZE(content_keys);

            /* Pre-calculate total ckeys string length */
            Py_ssize_t total_len = 0;
            for (Py_ssize_t j = 0; j < num_ckeys; j++) {
                if (j > 0) total_len++;  /* comma */
                total_len += PyUnicode_GET_LENGTH(
                    PyList_GET_ITEM(content_keys, j));
            }

            /* Build ckeys string efficiently */
            char *ckeys_buf = (char *)PyMem_Malloc(total_len + 1);
            if (!ckeys_buf) {
                Py_DECREF(content_dict);
                Py_DECREF(content_keys);
                Py_DECREF(content_val);
                PyErr_NoMemory();
                goto error_rd;
            }
            Py_ssize_t ckeys_pos = 0;
            for (Py_ssize_t j = 0; j < num_ckeys; j++) {
                PyObject *ck = PyList_GET_ITEM(content_keys, j);
                const char *cks = PyUnicode_AsUTF8(ck);
                Py_ssize_t cklen = PyUnicode_GET_LENGTH(ck);

                if (j > 0)
                    ckeys_buf[ckeys_pos++] = ',';
                memcpy(ckeys_buf + ckeys_pos, cks, cklen);
                ckeys_pos += cklen;

                /* Add "c.xxx" -> value to row_dict */
                PyObject *cval = PyDict_GetItem(content_dict, ck);  /* borrowed */
                PyObject *cprefixed = PyUnicode_FromFormat("c.%s", cks);
                if (cprefixed) {
                    PyDict_SetItem(row_dict, cprefixed, cval);
                    Py_DECREF(cprefixed);
                }
            }
            ckeys_str = PyUnicode_FromStringAndSize(ckeys_buf, ckeys_pos);
            PyMem_Free(ckeys_buf);

            Py_DECREF(content_dict);
            Py_DECREF(content_keys);
        } else {
            /* Non-object content — store as raw */
            PyObject *raw_key = PyUnicode_FromString("c.__raw__");
            if (raw_key) {
                PyDict_SetItem(row_dict, raw_key, content_val);
                Py_DECREF(raw_key);
            }
            ckeys_str = PyUnicode_FromString("__raw__");
        }
        Py_DECREF(content_val);
    } else {
        ckeys_str = PyUnicode_FromString("");
    }

    /* Add __ckeys__ to row_dict */
    {
        PyObject *ckeys_key = PyUnicode_FromString("__ckeys__");
        PyDict_SetItem(row_dict, ckeys_key, ckeys_str);
        Py_DECREF(ckeys_key);
        Py_DECREF(ckeys_str);
    }

    /* Clean up top-level dict (no longer needed) */
    Py_DECREF(top_dict);

    /* Return (record_type, row_dict, top_keys) */
    {
        PyObject *result = PyTuple_Pack(3, record_type, row_dict, top_keys);
        Py_DECREF(record_type);
        Py_DECREF(row_dict);
        Py_DECREF(top_keys);
        return result;
    }

    /* Error cleanup paths */
error_rd:
    Py_DECREF(row_dict);
error_rt:
    Py_DECREF(record_type);
error:
    Py_XDECREF(top_dict);
    Py_XDECREF(top_keys);
    return NULL;
}

/* ------------------------------------------------------------------ */
/* Python-exposed: parse_object_raw(s) -> (dict, end_pos, keys)       */
/* ------------------------------------------------------------------ */

static PyObject *py_parse_object_raw(PyObject *self, PyObject *args)
{
    const char *s;
    Py_ssize_t s_len;

    if (!PyArg_ParseTuple(args, "s#", &s, &s_len))
        return NULL;

    PyObject *dict = NULL, *keys = NULL;
    Py_ssize_t end_pos;
    if (parse_object(s, 0, s_len, &dict, &keys, &end_pos) < 0)
        return NULL;

    PyObject *pos_obj = PyLong_FromSsize_t(end_pos);
    if (!pos_obj) {
        Py_DECREF(dict);
        Py_DECREF(keys);
        return NULL;
    }

    PyObject *result = PyTuple_Pack(3, dict, pos_obj, keys);
    Py_DECREF(dict);
    Py_DECREF(pos_obj);
    Py_DECREF(keys);
    return result;
}

/* ------------------------------------------------------------------ */
/* Module definition                                                   */
/* ------------------------------------------------------------------ */

static PyMethodDef methods[] = {
    {"parse_line", py_parse_line, METH_VARARGS,
     "Parse a telemetry JSONL line.\n\n"
     "Returns (record_type, row_dict, top_keys) or None for blank lines."},
    {"parse_object_raw", py_parse_object_raw, METH_VARARGS,
     "Parse a JSON object returning (dict, end_pos, keys_list)."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_telemetry_scanner",
    "Fast C JSON scanner for telemetry codec — preserves exact numeric text.",
    -1,
    methods
};

PyMODINIT_FUNC
PyInit__telemetry_scanner(void)
{
    return PyModule_Create(&module);
}
