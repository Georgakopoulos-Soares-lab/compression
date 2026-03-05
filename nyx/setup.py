"""Build script for nyx — includes optional C extension for fast JSON scanning."""

from setuptools import setup, Extension

scanner_ext = Extension(
    "nyx.core._telemetry_scanner",
    sources=["nyx/core/_telemetry_scanner.c"],
)

setup(
    ext_modules=[scanner_ext],
)
