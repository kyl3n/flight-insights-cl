"""PyInstaller hook for pyarrow: only what the engine uses.

The engine uses arrays, IPC files and Parquet with modular encryption. pyarrow's
own hook takes every submodule and native library, including Flight RPC (about
30 MB), the S3, GCS, Azure and HDFS filesystems, ORC and its C++ test module.
Nothing the engine imports links those (checked with readelf on pyarrow 22:
only _flight and _s3fs need libarrow_flight and libarrow_s3), so they are left
out. Substrait, Dataset and Acero stay: pyarrow's core library links them. The
frozen engine's smoke test proves nothing needed was cut.

This hook is passed with --additional-hooks-dir, which takes precedence over the
one in pyinstaller-hooks-contrib.
"""

import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

UNUSED = re.compile(r"flight|s3|gcsfs|azurefs|hdfs|(^|[._])orc|cpp_tests|tests|gandiva|cuda")

hiddenimports = [m for m in collect_submodules("pyarrow") if not UNUSED.search(m)]


def _keep(item: tuple[str, str]) -> bool:
    return not UNUSED.search(Path(item[0]).name)


datas = [d for d in collect_data_files("pyarrow") if _keep(d)]
binaries = [b for b in collect_dynamic_libs("pyarrow") if _keep(b)]
excludedimports = ["pyarrow.flight", "pyarrow._flight", "pyarrow._s3fs", "pyarrow._gcsfs",
                   "pyarrow._azurefs", "pyarrow._hdfs", "pyarrow.orc", "pyarrow._orc",
                   "pyarrow._dataset_orc", "pyarrow._pyarrow_cpp_tests"]  # fmt: skip
