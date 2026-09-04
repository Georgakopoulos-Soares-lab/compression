#!/usr/bin/env bash
# get_validation_corpus.sh — fetch a diverse set of FASTA/VCF files plus generate
# edge cases, for scripts/validate_roundtrip.sh. Small (~30 MB total).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
C="$HERE/data/validation"
mkdir -p "$C/fasta" "$C/vcf"

dl() { [ -s "$2" ] || { echo "  get $(basename "$2")"; curl -fL --retry 3 -o "$2" "$1"; }; }

echo "== FASTA downloads =="
dl "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/chromosomes/chr21.fa.gz"        "$C/fasta/ucsc_hg38_chr21.softmasked.fa.gz"   # soft-masked genome
dl "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/chromosomes/chrM.fa.gz"         "$C/fasta/ucsc_hg38_chrM.fa.gz"               # tiny
dl "https://rest.uniprot.org/uniprotkb/stream?query=proteome:UP000000625&format=fasta&compressed=true" "$C/fasta/uniprot_ecoli_proteome.fa.gz"  # protein
dl "https://ftp.ensembl.org/pub/release-110/fasta/saccharomyces_cerevisiae/cdna/Saccharomyces_cerevisiae.R64-1-1.cdna.all.fa.gz" "$C/fasta/ensembl_yeast_cdna.fa.gz"  # transcripts

echo "== decompress =="
for g in "$C"/fasta/*.gz; do [ -s "${g%.gz}" ] || gzip -dkf "$g"; done

echo "== generated FASTA edge cases =="
G="$C/fasta"
printf '>seq1 crlf endings\r\nACGTacgtNNNN\r\nRYSWKMBDHV\r\n'                       > "$G/edge_crlf.fasta"
printf '>a\nACGT\n\n\nNNNN\n>b no seq\n>c\nacgtACGT'                                > "$G/edge_blanklines_notrailnl.fasta"
printf '; leading comment\n; another\n>x\n%s\n' "$(python3 -c "print('ACGTacgtNRYWSKM'*4000)")" > "$G/edge_preamble_iupac.fasta"
python3 - "$G/edge_irregular_widths.fasta" <<'PY'
import random; random.seed(7)
w=open(__import__('sys').argv[1],'w')
for r in range(20):
    w.write(f">rec{r}\n")
    s=''.join(random.choice('ACGTacgtN') for _ in range(random.randint(50,900)))
    i=0
    while i<len(s):
        L=random.randint(1,120); w.write(s[i:i+L]+"\n"); i+=L
PY
printf '>only lowercase\n%s\n' "$(python3 -c "print('acgtacgtacgt'*500)")"           > "$G/edge_lowercase.fasta"
head -c 4000000 "$G/ucsc_hg38_chr21.softmasked.fa" | tr -d '\n' > "$G/edge_singleline.fasta" || true
printf '\n' >> "$G/edge_singleline.fasta"

echo "== VCF downloads =="
dl "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz"             "$C/vcf/clinvar_grch38.vcf.gz"
# real (tiny) GATK HaplotypeCaller gVCFs — structural validation of the gvcf path
dl "https://raw.githubusercontent.com/nf-core/test-datasets/modules/data/genomics/homo_sapiens/illumina/gvcf/test.genome.vcf.gz"  "$C/vcf/nfcore_test.g.vcf.gz"
dl "https://raw.githubusercontent.com/nf-core/test-datasets/modules/data/genomics/homo_sapiens/illumina/gvcf/test2.genome.vcf.gz" "$C/vcf/nfcore_test2.g.vcf.gz"
for g in "$C"/vcf/*.gz; do [ -s "${g%.gz}" ] || gzip -dkf "$g"; done

echo
echo "corpus at $C :"
find "$C" -type f ! -name '*.gz' -printf '  %-55p %10s B\n' | sort
