"""Stand-in for chdman in tests: `fake_chdman.py createcd -i in.cue -o out.chd`.

Fails (exit 1) when the input path contains "FAIL"; otherwise writes a small
fake CHD whose content records the mode and the input name.
"""
import sys
from pathlib import Path

args = sys.argv[1:]
mode = args[0]
inp = Path(args[args.index("-i") + 1])
out = Path(args[args.index("-o") + 1])
if "FAIL" in inp.name:
    print("Error: synthetic chdman failure for " + inp.name, file=sys.stderr)
    sys.exit(1)
if out.exists():
    print("Error: file already exists", file=sys.stderr)
    sys.exit(1)
if mode == "extractcd":
    ob = Path(args[args.index("-ob") + 1])
    ob.write_bytes(b"\0" * 2352 * 2)
    out.write_text(f'FILE "{ob.name}" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
    sys.exit(0)
if mode == "extractdvd":
    out.write_bytes(b"ISO" + inp.read_bytes())
    sys.exit(0)
for pct in (10, 50, 100):
    print(f"Compressing, {pct}.0% complete... (ratio=50.0%)", end="\r", flush=True)
print("\nCompression complete ... final ratio = 50.0%")
out.write_bytes(b"MComprHD" + mode.encode() + b":" + inp.name.encode())
