#!/usr/bin/env bash
# Run the altitude benchmark on the HERIDAL test split in single-pass and
# tiled modes, then draw docs/img/altitude_recall.png.
#
# Usage: scripts/run_altitude_benchmark.sh
# Environment:
#   DATASETS_DIR  parent of heridal/ (default: datasets)
#   PYTHON        interpreter to use (default: python)
#
# The full HERIDAL zip is 8.3 GB. When the test split is not on disk yet, this
# script fetches only the 101 test images and their annotations from the zip on
# Zenodo with HTTP range requests (about 0.5 GB) instead of the whole archive.
# scripts/download_heridal.sh downloads the complete archive if you need it.
set -euo pipefail

PYTHON="${PYTHON:-python}"
DATA="${DATASETS_DIR:-datasets}/heridal/test"

if [ ! -d "${DATA}/JPEGImages" ] || [ "$(ls "${DATA}/JPEGImages" 2>/dev/null | wc -l)" -lt 101 ]; then
  echo "Fetching the HERIDAL test split into ${DATA}"
  "${PYTHON}" - "${DATA}" <<'PYFETCH'
import io,sys,urllib.request,zipfile
U="https://zenodo.org/records/5662351/files/heridal_keras_retinanet_voc.zip?download=1"
SIZE=8311735812
class R(io.RawIOBase):
    def __init__(s): s.p=0
    def seekable(s): return True
    def readable(s): return True
    def tell(s): return s.p
    def seek(s,o,w=0):
        s.p = o if w==0 else s.p+o if w==1 else SIZE+o
        return s.p
    def readinto(s,b):
        d=s.read(len(b)); b[:len(d)]=d; return len(d)
    def read(s,n=-1):
        if n<0: n=SIZE-s.p
        n=min(n,SIZE-s.p)
        if n<=0: return b""
        req=urllib.request.Request(U,headers={"Range":f"bytes={s.p}-{s.p+n-1}"})
        import time
        for a in range(8):
            try:
                d=urllib.request.urlopen(req,timeout=300).read(); break
            except Exception as e:
                print('retry',a,e,file=sys.stderr); time.sleep(3)
        else: raise IOError('range failed')
        s.p+=len(d); return d
import io,zipfile,sys,pathlib,struct,zlib,time,urllib.request
from concurrent.futures import ThreadPoolExecutor
out=pathlib.Path(sys.argv[1])
z=zipfile.ZipFile(io.BufferedReader(R(),buffer_size=1<<20))
P="heridal_keras_retinanet_voc/"
names=z.read(P+"ImageSets/Main/test.txt").decode().split()
info={i.filename:i for i in z.infolist()}
def fetch(a,b):
    for t in range(10):
        try:
            return urllib.request.urlopen(urllib.request.Request(U,headers={"Range":f"bytes={a}-{b}"}),timeout=300).read()
        except Exception as e:
            print("retry",e,file=sys.stderr); time.sleep(3)
    raise IOError
def get(zi):
    raw=fetch(zi.header_offset,zi.header_offset+30+512+zi.compress_size+4096)
    n,m=struct.unpack("<HH",raw[26:30]); body=raw[30+n+m:30+n+m+zi.compress_size]
    if zi.compress_type==0: d=body
    else: d=zlib.decompress(body,-15)
    assert len(d)==zi.file_size,(zi.filename,len(d),zi.file_size)
    return d
def job(a):
    sub,ext,i=a
    dst=out/sub/(i+ext)
    if dst.exists() and dst.stat().st_size>0: return
    dst.write_bytes(get(info[P+f"{sub}/{i}{ext}"])); print("ok",i,ext,flush=True)
for s in("JPEGImages","Annotations"): (out/s).mkdir(parents=True,exist_ok=True)
jobs=[(s,e,i) for i in names for s,e in(("Annotations",".xml"),("JPEGImages",".jpg"))]
with ThreadPoolExecutor(8) as ex: list(ex.map(job,jobs))
print("DONE")
PYFETCH
fi

mkdir -p results docs/img
"${PYTHON}" -m flightrisk.evaluation detect --dataset "${DATA}" --tiled 0 --output results/altitude_single.json
"${PYTHON}" -m flightrisk.evaluation detect --dataset "${DATA}" --tiled 1 --output results/altitude_tiled.json

"${PYTHON}" - <<'PYPLOT'
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(7, 4.2), dpi=150)
for name, label, color in (("single", "Single pass", "#c0392b"), ("tiled", "Tiled (640 px tiles)", "#1f77b4")):
    data = json.load(open(f"results/altitude_{name}.json"))
    bins = data["bins"]
    labels = [f"[{b['lo']:g},{'inf' if b['hi'] is None else format(b['hi'], 'g')})" for b in bins]
    ax.plot(range(len(bins)), [b["recall"] for b in bins], marker="o", label=label, color=color)
ax.axhline(0.5, color="gray", linestyle="--", linewidth=1, label="Recall 0.5 floor")
ax.set_xticks(range(len(labels)))
ax.set_xticklabels(labels, rotation=20)
ax.set_ylim(0, 1.05)
ax.set_xlabel("Person height in the frame (pixels)")
ax.set_ylabel("Recall (IoU 0.3)")
ax.set_title("Person detection recall by pixel height, HERIDAL test split")
ax.grid(alpha=0.3)
ax.legend()
fig.tight_layout()
fig.savefig("docs/img/altitude_recall.png")
PYPLOT
