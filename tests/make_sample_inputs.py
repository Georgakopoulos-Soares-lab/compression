#!/usr/bin/env python3
"""Write one small, realistic-looking file per format into <dir>, deterministically.

  make_sample_inputs.py <dir> [scale]

Used by tests that need real structure (compressible genotypes, quality strings,
peak tables) rather than the edge cases the round-trip suites generate. Fixed
seed, so every run and every CI machine gets the same bytes.
"""
import os, random, sys

d = sys.argv[1]; scale = int(sys.argv[2]) if len(sys.argv) > 2 else 1
os.makedirs(d, exist_ok=True)
rng = random.Random(20260926)
ACGT = "ACGT"

with open(os.path.join(d, "sample.fa"), "w") as f:
    for c in range(3):
        f.write(f">chr{c+1} synthetic\n")
        seq = [rng.choice(ACGT) for _ in range(300000 * scale)]
        for i in range(0, len(seq), 60):
            f.write("".join(seq[i:i+60]) + "\n")

with open(os.path.join(d, "sample.fastq"), "w") as f:
    for i in range(20000 * scale):
        n = 100
        f.write(f"@SIM:1:FC:1:{1101 + i // 5000}:{rng.randrange(30000)}:{rng.randrange(30000)} 1:N:0:ACGT\n")
        f.write("".join(rng.choice(ACGT) for _ in range(n)) + "\n+\n")
        f.write("".join(rng.choice("FFFFF:,") for _ in range(n)) + "\n")

with open(os.path.join(d, "sample.vcf"), "w") as f:
    f.write("##fileformat=VCFv4.2\n##INFO=<ID=DP,Number=1,Type=Integer,Description=\"Depth\">\n")
    f.write("##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n")
    samples = [f"S{i}" for i in range(50)]
    f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + "\t".join(samples) + "\n")
    pos = 10000
    for i in range(20000 * scale):
        pos += rng.randrange(1, 400)
        ref = rng.choice(ACGT); alt = rng.choice([b for b in ACGT if b != ref])
        af = rng.random() * 0.3
        gts = "\t".join(f"{int(rng.random() < af)}|{int(rng.random() < af)}" for _ in samples)
        f.write(f"chr1\t{pos}\t.\t{ref}\t{alt}\t{rng.randrange(20, 99)}\tPASS\tDP={rng.randrange(10, 60)}\tGT\t{gts}\n")

with open(os.path.join(d, "sample.bed"), "w") as f:
    states = [("1_TssA", "255,0,0"), ("5_TxWk", "0,100,0"), ("9_Het", "138,145,208"), ("15_Quies", "255,255,255")]
    pos = 0
    for i in range(60000 * scale):
        ln = rng.randrange(200, 5000); s, rgb = rng.choice(states)
        f.write(f"chr1\t{pos}\t{pos+ln}\t{s}\t0\t.\t{pos}\t{pos+ln}\t{rgb}\n"); pos += ln
