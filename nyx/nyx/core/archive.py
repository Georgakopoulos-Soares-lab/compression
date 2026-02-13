"""Read and write .nyx archive files (tar-based)."""

import json
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


NYX_ARCHIVE_VERSION = 1


def create_archive(
    output_path: Path,
    manifest: Dict[str, Any],
    chunk_files: List[Path],
    compressor_path: Optional[Path] = None,
) -> Path:
    """Create a .nyx archive bundling compressed chunks and metadata.

    Archive layout:
        manifest.json
        compressor.model  (if applicable)
        chunks/
            chunk_00000.*.zl
            chunk_00001.*.zl
            ...

    Args:
        output_path: Path for the output .nyx file.
        manifest: Metadata dictionary to serialize as manifest.json.
        chunk_files: List of compressed chunk file paths.
        compressor_path: Optional path to trained compressor file.

    Returns:
        Path to the created archive.
    """
    manifest["version"] = NYX_ARCHIVE_VERSION

    with tarfile.open(output_path, "w") as tar:
        # Add manifest
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, fileobj=_bytes_io(manifest_bytes))

        # Add compressor if present
        if compressor_path and compressor_path.is_file():
            tar.add(str(compressor_path), arcname="compressor.model")

        # Add compressed chunks
        for chunk in sorted(chunk_files):
            arcname = f"chunks/{chunk.name}"
            tar.add(str(chunk), arcname=arcname)

    return output_path


def extract_archive(archive_path: Path, extract_dir: Path) -> Dict[str, Any]:
    """Extract a .nyx archive and return the manifest.

    Args:
        archive_path: Path to the .nyx archive file.
        extract_dir: Directory to extract contents into.

    Returns:
        The parsed manifest dictionary.

    Raises:
        ValueError: If the archive is invalid or missing manifest.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r") as tar:
        # Security: prevent path traversal
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in member.name:
                raise ValueError(
                    f"Archive contains unsafe path: {member.name}"
                )
        tar.extractall(extract_dir)

    manifest_path = extract_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("Archive missing manifest.json")

    with open(manifest_path) as f:
        manifest = json.load(f)

    return manifest


def get_chunks_from_extract(extract_dir: Path) -> List[Path]:
    """Get sorted list of chunk files from an extracted archive."""
    chunks_dir = extract_dir / "chunks"
    if not chunks_dir.is_dir():
        return []
    return sorted(chunks_dir.iterdir())


def get_compressor_from_extract(extract_dir: Path) -> Optional[Path]:
    """Get the compressor file from an extracted archive, if present."""
    compressor = extract_dir / "compressor.model"
    if compressor.is_file():
        return compressor
    return None


def _bytes_io(data: bytes):
    """Create a BytesIO wrapper for tarfile.addfile."""
    import io
    return io.BytesIO(data)
