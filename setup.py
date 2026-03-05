"""Build the C extension for fast JSON scanning."""

from setuptools import setup, Extension

scanner_ext = Extension(
    "_telemetry_scanner",
    sources=["_telemetry_scanner.c"],
)

setup(
    py_modules=[],
    packages=[],
    ext_modules=[scanner_ext],
)
