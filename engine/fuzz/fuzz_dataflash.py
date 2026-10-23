"""Coverage-guided fuzzing of the DataFlash decoder with Atheris.

Logs are untrusted input. This runs the whole decode path on mutated logs and
fails on any exception other than the documented ValueError for a type redefined
with different columns. Run by .github/workflows/fuzz.yml; locally:

    uv run --with atheris==3.1.0 python fuzz/fuzz_dataflash.py -max_total_time=300 corpus/
"""

import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports():
    from flight_insights.dataflash import read_dataflash
    from flight_insights.dataflash.compat import pymavlink_messages

WORK = Path(tempfile.mkdtemp())


def test_one_input(data: bytes) -> None:
    path = WORK / "input.bin"
    path.write_bytes(data)
    with read_dataflash(path) as log:
        for _ in pymavlink_messages(log):
            pass
        for name in log.message_types:
            try:
                log.table(name)
            except ValueError as e:
                if "different columns" not in str(e):
                    raise


if __name__ == "__main__":
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
