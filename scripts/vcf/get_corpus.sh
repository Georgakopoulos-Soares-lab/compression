#!/usr/bin/env bash
# get_corpus.sh — build a validation corpus of >=3 VCFs per archetype for the
# auto-detect accuracy + compression tables in the paper.
#
#   scripts/vcf/get_corpus.sh [--force]
#
# Every file is REAL data. Downloads what is publicly available; the trio /
# cohort sizes and the sites-only projections are cut from the local 1000G
# phase-3 panels with bcftools -- real genotypes, only the sample set is subset,
# so they are labelled "derived" (not "synthetic"). No synthetic VCFs: if real
# data for an archetype is unavailable it is left short and noted, never faked.
#
# Writes:  data/vcf/corpus/<archetype>__<tag>.vcf[.gz]
#          data/vcf/corpus/manifest.tsv   (archetype <TAB> path <TAB> status <TAB> note)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BCF="$HERE/bcftools-1.19/bcftools"
OUT="$HERE/data/vcf/corpus"
MAN="$OUT/manifest.tsv"
FORCE=0; [ "${1:-}" = --force ] && FORCE=1
mkdir -p "$OUT"

CHR22="$HERE/data/vcf/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf"
CHR21="$HERE/data/vcf/ALL.chr21.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf"
CHR20="$HERE/data/vcf/ALL.chr20.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz"
HG002="$HERE/data/test/HG002_GRCh38_1_22_v4.2.1_benchmark.vcf"

: > "$MAN"
add() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$MAN"; echo "  + $1  $(basename "$2")  ($3)"; }
have() { [ "$FORCE" = 0 ] && [ -s "$1" ]; }
dl() { # url dest
  have "$2" && { echo "  have $(basename "$2")"; return 0; }
  echo "  dl $(basename "$2")"
  curl -fL --retry 3 --retry-delay 5 --max-time 3600 -o "$2.part" "$1" && mv "$2.part" "$2"
}

echo "== panel (>=1000 samples) =="
[ -s "$CHR22" ] && add panel "$CHR22" real "1000G phase3 chr22, 2504 samples"
[ -s "$CHR21" ] && add panel "$CHR21" real "1000G phase3 chr21, 2504 samples"
if dl "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chr20.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz" "$CHR20"; then
  add panel "$CHR20" real "1000G phase3 chr20, 2504 samples"
fi

echo "== derive trio / cohort from the local panels (real genotypes, subset samples) =="
if [ -x "$BCF" ]; then
  for spec in "chr22:$CHR22" "chr21:$CHR21" "chr20:$CHR20"; do
    tag="${spec%%:*}"; src="${spec#*:}"
    [ -s "$src" ] || continue
    mapfile -t S < <("$BCF" query -l "$src" 2>/dev/null)
    [ "${#S[@]}" -ge 500 ] || continue
    # family-trio: 3, 6, 10 samples across the three chromosomes
    case "$tag" in chr22) k=3;; chr21) k=6;; *) k=10;; esac
    ft="$OUT/family-trio__${tag}_${k}samp.vcf"
    if ! have "$ft"; then s=$(IFS=,; echo "${S[*]:0:$k}"); "$BCF" view -s "$s" "$src" -Ov -o "$ft" 2>/dev/null; fi
    [ -s "$ft" ] && add family-trio "$ft" derived "1000G $tag, first $k samples"
    # cohort: 100, 250, 500
    case "$tag" in chr22) n=100;; chr21) n=250;; *) n=500;; esac
    co="$OUT/cohort__${tag}_${n}samp.vcf"
    if ! have "$co"; then s=$(IFS=,; echo "${S[*]:0:$n}"); "$BCF" view -s "$s" "$src" -Ov -o "$co" 2>/dev/null; fi
    [ -s "$co" ] && add cohort "$co" derived "1000G $tag, first $n samples"
    # sites-frequency: drop genotypes -> AF/AC/AN sites file
    sf="$OUT/sites-frequency__${tag}_1000G.vcf"
    if ! have "$sf"; then "$BCF" view -G "$src" -Ov -o "$sf" 2>/dev/null; fi
    [ -s "$sf" ] && add sites-frequency "$sf" real "1000G $tag sites-only (AF/AC/AN), genotypes dropped"
  done
fi

echo "== sites-frequency : gnomAD v4 (download) =="
G1="$OUT/sites-frequency__gnomad_v4_exomes_chrY.vcf.bgz"
G2="$OUT/sites-frequency__gnomad_v4_genomes_chrY.vcf.bgz"
dl "https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/exomes/gnomad.exomes.v4.1.sites.chrY.vcf.bgz" "$G1" \
  && add sites-frequency "$G1" real "gnomAD v4.1 exomes sites, chrY"
dl "https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/genomes/gnomad.genomes.v4.1.sites.chrY.vcf.bgz" "$G2" \
  && add sites-frequency "$G2" real "gnomAD v4.1 genomes sites, chrY"

echo "== sites-annotated : ClinVar (GRCh38 + GRCh37) + CIViC =="
CV38="$HERE/data/vcf/clinvar.vcf.gz"
CV37="$OUT/sites-annotated__clinvar_GRCh37.vcf.gz"
CIV="$OUT/sites-annotated__civic_nightly.vcf"
[ -s "$CV38" ] && add sites-annotated "$CV38" real "NCBI ClinVar GRCh38"
dl "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh37/clinvar.vcf.gz" "$CV37" \
  && add sites-annotated "$CV37" real "NCBI ClinVar GRCh37"
dl "https://civicdb.org/downloads/nightly/nightly-civic_accepted_and_submitted.vcf" "$CIV" \
  && add sites-annotated "$CIV" real "CIViC clinical interpretations (nightly)"

echo "== single-sample : GIAB HG002 / HG003 / HG004 =="
[ -s "$HG002" ] && add single-sample "$HG002" real "GIAB HG002 GRCh38 v4.2.1 benchmark"
gb="https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/release/AshkenazimTrio"
for spec in \
  "HG003:$gb/HG003_NA24149_father/NISTv4.2.1/GRCh38/HG003_GRCh38_1_22_v4.2.1_benchmark.vcf.gz" \
  "HG004:$gb/HG004_NA24143_mother/NISTv4.2.1/GRCh38/HG004_GRCh38_1_22_v4.2.1_benchmark.vcf.gz"; do
  s="${spec%%:*}"; u="${spec#*:}"; d="$OUT/single-sample__${s}_GRCh38_v4.2.1.vcf.gz"
  dl "$u" "$d" && add single-sample "$d" real "GIAB $s GRCh38 v4.2.1 benchmark"
done

echo "== somatic : SEQC2 HCC1395 (all real caller output) =="
SS="$HERE/data/vcf/seqc2_hcc1395_ssnv.vcf.gz"
[ -s "$SS" ] && add somatic "$SS" real "SEQC2 HCC1395 high-confidence sSNV (v1.2.1)"
SI="$OUT/somatic__seqc2_hcc1395_sindel.vcf.gz"
dl "https://ftp.ncbi.nlm.nih.gov/ReferenceSamples/seqc/Somatic_Mutation_WG/release/latest/high-confidence_sINDEL_in_HC_regions_v1.2.1.vcf.gz" "$SI" \
  && add somatic "$SI" real "SEQC2 HCC1395 high-confidence sINDEL (v1.2.1)"
SU="$OUT/somatic__seqc2_hcc1395_ssnv_superset.vcf.gz"
dl "https://ftp.ncbi.nlm.nih.gov/ReferenceSamples/seqc/Somatic_Mutation_WG/release/latest/sSNV.MSDUKT.superSet.v1.2.vcf.gz" "$SU" \
  && add somatic "$SU" real "SEQC2 HCC1395 sSNV multi-caller superset (MuSE/Strelka/SomaticSniper/VarScan/MuTect2/TNscope, v1.2)"

echo "== gvcf : real GATK HaplotypeCaller output (nf-core test-datasets) =="
# Real gVCFs of meaningful size are not otherwise downloadable over plain HTTP
# (public ones live in gs:// requester-pays / auth-gated buckets). These are
# genuine `gatk HaplotypeCaller -ERC GVCF` outputs: real <NON_REF> alleles,
# real END= reference blocks, real MIN_DP/PL. Small (chr21/22 test regions).
gvbase="https://raw.githubusercontent.com/nf-core/test-datasets/modules/data/genomics/homo_sapiens/illumina"
for spec in \
  "gatk_hc_calls:$gvbase/gatk/haplotypecaller_calls/test.g.vcf.gz" \
  "gatk_hc_calls2:$gvbase/gatk/haplotypecaller_calls/test2.g.vcf.gz" \
  "gatk_hc_genome:$gvbase/gvcf/test.genome.g.vcf.gz"; do
  tag="${spec%%:*}"; u="${spec#*:}"; d="$OUT/gvcf__nfcore_${tag}_real.g.vcf.gz"
  dl "$u" "$d" && add gvcf "$d" real "real GATK HaplotypeCaller gVCF ($tag), nf-core test-datasets"
done

echo
echo "corpus manifest: $MAN"
awk -F'\t' '{c[$1]++} END{for(a in c) printf "  %-16s %d files\n", a, c[a]}' "$MAN" | sort
