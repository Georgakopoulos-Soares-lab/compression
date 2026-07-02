import os
import subprocess
import csv
import time
import glob
import shutil
import argparse
import filecmp

# ==========================================
# BENCHMARKING & METRICS UTILITIES
# ==========================================
def get_file_size(filepath):
    if filepath and os.path.exists(filepath):
        return os.path.getsize(filepath)
    return 0

def run_with_metrics(cmd_list, output_file=None):
    time_cmd = ["/usr/bin/time", "-v"] + cmd_list
    start_t = time.time()
    res = subprocess.run(time_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    end_t = time.time()
    
    time_sec = round(end_t - start_t, 2)
    size = get_file_size(output_file) if output_file else 0
    
    mem_mb = 0.0
    stderr_str = res.stderr.decode('utf-8', errors='ignore')
    for line in stderr_str.split('\n'):
        if "Maximum resident set size" in line:
            parts = line.split(':')
            if len(parts) > 1:
                mem_mb = round(int(parts[-1].strip()) / 1024, 2)
            break
            
    return size, time_sec, mem_mb, res.returncode

def execute_tool_benchmark(tool_name, comp_cmd, decomp_cmd, out_comp_file, out_decomp_file, orig_size, orig_file):
    print(f"     Testing {tool_name}...")
    comp_size, comp_time, comp_mem, c_ret = run_with_metrics(comp_cmd, out_comp_file)
    ratio = round(orig_size / comp_size, 2) if comp_size > 0 and c_ret == 0 else 0
    
    decomp_time, decomp_mem = 0.0, 0.0
    is_valid = False
    if c_ret == 0 and decomp_cmd:
        _, decomp_time, decomp_mem, d_ret = run_with_metrics(decomp_cmd, out_decomp_file)
        if d_ret == 0:
            is_valid = filecmp.cmp(orig_file, out_decomp_file, shallow=False)
    
    if os.path.exists(out_comp_file): os.remove(out_comp_file)
    if os.path.exists(out_decomp_file): os.remove(out_decomp_file)
    
    return {
        f"{tool_name}_Ratio": ratio if c_ret == 0 else "FAIL",
        f"{tool_name}_Comp_sec": comp_time,
        f"{tool_name}_Decomp_sec": decomp_time,
        f"{tool_name}_Valid": "PASS" if is_valid else "FAIL"
    }

def run_external_benchmarks(filepath, orig_size, threads):
    benchmarks = {}
    benchmarks.update(execute_tool_benchmark("ZSTD", ["bash", "-c", f"zstd -19 -k -f -T{threads} {filepath} -o {filepath}.zst"], ["bash", "-c", f"zstd -d -f {filepath}.zst -o {filepath}.dec"], f"{filepath}.zst", f"{filepath}.dec", orig_size, filepath))
    benchmarks.update(execute_tool_benchmark("GZIP", ["bash", "-c", f"pigz -9 -p {threads} -c {filepath} > {filepath}.gz"], ["bash", "-c", f"pigz -d -p {threads} -c {filepath}.gz > {filepath}.dec"], f"{filepath}.gz", f"{filepath}.dec", orig_size, filepath))
    benchmarks.update(execute_tool_benchmark("XZ", ["bash", "-c", f"xz -9 -T{threads} -k -f {filepath}"], ["bash", "-c", f"xz -d -T{threads} -k -f {filepath}.xz -c > {filepath}.dec"], f"{filepath}.xz", f"{filepath}.dec", orig_size, filepath))
    benchmarks.update(execute_tool_benchmark("Genozip", ["genozip", filepath, "--force", "-o", f"{filepath}.genozip"], ["genounzip", f"{filepath}.genozip", "--force", "-o", f"{filepath}.dec"], f"{filepath}.genozip", f"{filepath}.dec", orig_size, filepath))
    benchmarks.update(execute_tool_benchmark("SPRING", ["spring", "-c", "-t", str(threads), "-i", filepath, "-o", f"{filepath}.spring"], ["spring", "-d", "-t", str(threads), "-i", f"{filepath}.spring", "-o", f"{filepath}.dec"], f"{filepath}.spring", f"{filepath}.dec", orig_size, filepath))
    return benchmarks

def run_fallback_zstd(filepath, threads):
    print(f"     [!] Engaging ZSTD Fallback for {filepath}...")
    orig_size = get_file_size(filepath)
    comp_size, comp_time, _, _ = run_with_metrics(["bash", "-c", f"zstd -19 -k -f -T{threads} {filepath} -o {filepath}.zst.fallback"], f"{filepath}.zst.fallback")
    dec_size, dec_time, _, d_ret = run_with_metrics(["bash", "-c", f"zstd -d -f {filepath}.zst.fallback -o {filepath}.dec.fallback"], f"{filepath}.dec.fallback")
    
    is_valid = filecmp.cmp(filepath, f"{filepath}.dec.fallback", shallow=False) if d_ret == 0 else False
    
    if os.path.exists(f"{filepath}.zst.fallback"): os.remove(f"{filepath}.zst.fallback")
    if os.path.exists(f"{filepath}.dec.fallback"): os.remove(f"{filepath}.dec.fallback")
    
    ratio = round(orig_size / comp_size, 2) if comp_size > 0 else 0
    return ratio, comp_time, dec_time, is_valid

# ==========================================
# FORMAT PEEKERS
# ==========================================
def peek_vcf_stats(filepath):
    open_func = open
    if filepath.endswith('.gz'):
        import gzip
        open_func = gzip.open
    with open_func(filepath, 'rt') as f:
        for line in f:
            if not line.startswith('#'):
                cols = line.strip().split('\t')
                return len(cols), '|' in line
    return 0, False

def peek_fastq_read_length(filepath):
    open_func = open
    if filepath.endswith('.gz'):
        import gzip
        open_func = gzip.open
    with open_func(filepath, 'rt') as f:
        f.readline()
        return len(f.readline().strip())

# ==========================================
# PIPELINES
# ==========================================
def profile_vcf(vcf_path, threads, run_benchmark):
    print(f"\n[VCF] Profiling: {vcf_path}")
    orig_size = get_file_size(vcf_path)
    base_name = os.path.basename(vcf_path)
    
    num_cols, is_phased = peek_vcf_stats(vcf_path)
    subtype_str = f"{num_cols} Cols, {'Phased' if is_phased else 'Unphased'}"
    
    results = {"File": base_name, "Type": "VCF", "Schema/Format": subtype_str, "Original_MB": round(orig_size / (1024*1024), 2)}
    if run_benchmark: results.update(run_external_benchmarks(vcf_path, orig_size, threads))

    pack_dir = f"{vcf_path}_pack"
    os.makedirs(pack_dir, exist_ok=True)
    
    try:
        if subprocess.run(["./tools/vcf_preprocessing", vcf_path, pack_dir, "--threads", str(threads), "--max-chunk-mib", "40", "--delta-pos", "--dict-info", "--force"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE).returncode != 0: 
            raise RuntimeError("Preprocessor failed")

        model_path = f"artifacts/model_{num_cols}_cols.zlc"
        body_parts_dir = os.path.join(pack_dir, "body_parts")
        
        if not os.path.exists(model_path):
            train_tmp_dir = os.path.join(pack_dir, "train_tmp")
            os.makedirs(train_tmp_dir, exist_ok=True)
            parts = sorted([f for f in os.listdir(body_parts_dir) if f.endswith(".vcfbody")])
            for chunk in parts[:min(len(parts), max(10, int(len(parts) * 0.10)))]:
                shutil.copy(os.path.join(body_parts_dir, chunk), train_tmp_dir)
            subprocess.run(["./openzl/zli", "train", train_tmp_dir, "--profile", "csv", "--profile-arg", "\t", "--output", model_path, "--force", "--threads", str(threads), "--use-all-samples"], stdout=subprocess.DEVNULL)
            shutil.rmtree(train_tmp_dir)

        zl_dir = os.path.join(pack_dir, "zl")
        os.makedirs(zl_dir, exist_ok=True)
        
        # 1. COMPRESS
        start_time = time.time()
        for part_file in sorted([f for f in os.listdir(body_parts_dir) if f.endswith(".vcfbody")]):
            if subprocess.run(["./openzl/zli", "compress", os.path.join(body_parts_dir, part_file), "--compressor", model_path, "--output", os.path.join(zl_dir, f"{part_file}.zl"), "--force"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0:
                raise RuntimeError("OpenZL Engine Crash")
        zl_time = round(time.time() - start_time, 2)
        
        # 2. DECOMPRESS
        dec_start_time = time.time()
        for part_file in sorted([f for f in os.listdir(body_parts_dir) if f.endswith(".vcfbody")]):
            zl_path = os.path.join(zl_dir, f"{part_file}.zl")
            dec_path = os.path.join(body_parts_dir, f"{part_file}.dec")
            if subprocess.run(["./openzl/zli", "decompress", zl_path, "--output", dec_path, "--force"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0:
                raise RuntimeError("OpenZL Decompression Crash")
                
        # 3. RECONSTRUCT & VALIDATE
        recon_vcf = os.path.join(pack_dir, "recon.vcf")
        if subprocess.run(["./tools/vcf_postprocess", pack_dir, recon_vcf, "--threads", str(threads), "--chunk-suffix", ".dec"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0:
            raise RuntimeError("VCF Postprocess Crash")
            
        dec_time = round(time.time() - dec_start_time, 2)
        is_valid = filecmp.cmp(vcf_path, recon_vcf, shallow=False)
        
        zl_total_size = get_file_size(os.path.join(pack_dir, "header.vcf")) + sum(get_file_size(os.path.join(zl_dir, f)) for f in os.listdir(zl_dir))
        results.update({
            "OpenZL_Ratio": round(orig_size / zl_total_size, 2) if zl_total_size > 0 else 0, 
            "OpenZL_Comp_sec": zl_time,
            "OpenZL_Decomp_sec": dec_time,
            "OpenZL_Valid": "PASS" if is_valid else "FAIL"
        })

    except Exception as e:
        print(f"     [!] Pipeline failure: {str(e)}")
        fallback_ratio, fallback_comp, fallback_dec, fallback_valid = run_fallback_zstd(vcf_path, threads)
        results.update({"OpenZL_Ratio": f"FALLBACK_ZSTD ({fallback_ratio}x)", "OpenZL_Comp_sec": fallback_comp, "OpenZL_Decomp_sec": fallback_dec, "OpenZL_Valid": "PASS" if fallback_valid else "FAIL"})
            
    shutil.rmtree(pack_dir, ignore_errors=True)
    return results

def profile_fastq(fastq_path, threads, run_benchmark):
    print(f"\n[FASTQ] Profiling: {fastq_path}")
    orig_size = get_file_size(fastq_path)
    base_name = os.path.basename(fastq_path)
    
    read_length = peek_fastq_read_length(fastq_path)
    is_long_read = read_length > 300
    subtype_str = f"{'Long' if is_long_read else 'Short'} Read (~{read_length}bp)"
    
    results = {"File": base_name, "Type": "FASTQ", "Schema/Format": subtype_str, "Original_MB": round(orig_size / (1024*1024), 2)}
    if run_benchmark: results.update(run_external_benchmarks(fastq_path, orig_size, threads))
    
    if is_long_read:
        fallback_ratio, fallback_comp, fallback_dec, fallback_valid = run_fallback_zstd(fastq_path, threads)
        results.update({"OpenZL_Ratio": f"FALLBACK_ZSTD ({fallback_ratio}x)", "OpenZL_Comp_sec": fallback_comp, "OpenZL_Decomp_sec": fallback_dec, "OpenZL_Valid": "PASS" if fallback_valid else "FAIL"})
        return results

    pack_dir = f"{fastq_path}_pack"
    os.makedirs(pack_dir, exist_ok=True)
    pp_prefix = os.path.join(pack_dir, base_name)
    
    try:
        if subprocess.run(["./tools/fastq_preprocess", "encode", fastq_path, pp_prefix, str(threads), "--pack-4bit"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0: 
            raise RuntimeError("Preprocessor failed")

        body_parts_dir = os.path.join(pack_dir, "body_parts")
        os.makedirs(body_parts_dir, exist_ok=True)
        if os.path.exists(f"{pp_prefix}.tsv"):
            subprocess.run(["split", "-l", "500000", "-d", "--additional-suffix=.tsv", f"{pp_prefix}.tsv", os.path.join(body_parts_dir, "part_")])
            os.remove(f"{pp_prefix}.tsv")

        model_path = "artifacts/fastq_universal.compressor"
        if not os.path.exists(model_path): raise RuntimeError("Universal FASTQ model missing")

        zl_dir = os.path.join(pack_dir, "zl")
        os.makedirs(zl_dir, exist_ok=True)
        
        # 1. COMPRESS
        start_t = time.time()
        for part_file in sorted(os.listdir(body_parts_dir)):
            if subprocess.run(["./openzl/zli", "compress", os.path.join(body_parts_dir, part_file), "--compressor", model_path, "--output", os.path.join(zl_dir, f"{part_file}.zl"), "--force"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0: 
                raise RuntimeError("OpenZL Engine crash")
        zl_time = round(time.time() - start_t, 2)
        
        # 2. DECOMPRESS
        dec_start_time = time.time()
        recon_tsv = f"{pp_prefix}.tsv"
        with open(recon_tsv, 'wb') as wfd:
            for part_file in sorted([f for f in os.listdir(body_parts_dir) if f.startswith("part_")]):
                zl_path = os.path.join(zl_dir, f"{part_file}.zl")
                dec_path = os.path.join(body_parts_dir, f"{part_file}.dec")
                if subprocess.run(["./openzl/zli", "decompress", zl_path, "--output", dec_path, "--force"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0:
                    raise RuntimeError("OpenZL Decompression Crash")
                with open(dec_path, 'rb') as rfd:
                    shutil.copyfileobj(rfd, wfd)
                    
        # 3. RECONSTRUCT & VALIDATE
        recon_fastq = os.path.join(pack_dir, "recon.fastq")
        if subprocess.run(["./tools/fastq_preprocess", "decode", pp_prefix, recon_fastq, str(threads)], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0:
            raise RuntimeError("FASTQ Decode Crash")
            
        dec_time = round(time.time() - dec_start_time, 2)
        is_valid = filecmp.cmp(fastq_path, recon_fastq, shallow=False)

        zl_total_size = get_file_size(f"{pp_prefix}.meta") + sum(get_file_size(os.path.join(zl_dir, f)) for f in os.listdir(zl_dir))
        results.update({
            "OpenZL_Ratio": round(orig_size / zl_total_size, 2) if zl_total_size > 0 else 0, 
            "OpenZL_Comp_sec": zl_time,
            "OpenZL_Decomp_sec": dec_time,
            "OpenZL_Valid": "PASS" if is_valid else "FAIL"
        })
        
    except Exception as e:
        print(f"     [!] Pipeline failure: {str(e)}")
        fallback_ratio, fallback_comp, fallback_dec, fallback_valid = run_fallback_zstd(fastq_path, threads)
        results.update({"OpenZL_Ratio": f"FALLBACK_ZSTD ({fallback_ratio}x)", "OpenZL_Comp_sec": fallback_comp, "OpenZL_Decomp_sec": fallback_dec, "OpenZL_Valid": "PASS" if fallback_valid else "FAIL"})

    shutil.rmtree(pack_dir, ignore_errors=True)
    return results

def profile_fasta(fasta_path, threads, run_benchmark):
    print(f"\n[FASTA] Profiling: {fasta_path}")
    orig_size = get_file_size(fasta_path)
    base_name = os.path.basename(fasta_path)
    
    results = {"File": base_name, "Type": "FASTA", "Schema/Format": "FAV4 Packed", "Original_MB": round(orig_size / (1024*1024), 2)}
    if run_benchmark: results.update(run_external_benchmarks(fasta_path, orig_size, threads))
    
    pack_dir = f"{fasta_path}_pack"
    os.makedirs(pack_dir, exist_ok=True)
    
    try:
        if subprocess.run(["./tools/biocompress_preprocessor", fasta_path, pack_dir, str(threads), "fasta_packed"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0: 
            raise RuntimeError("FAV4 Preprocessor failed")
        
        models = glob.glob("artifacts/fasta_packed*.compressor")
        if not models: raise RuntimeError("FAV4 Model Missing")
        model_path = models[0]
        
        # 1. COMPRESS
        start_t = time.time()
        for part_file in sorted(glob.glob(os.path.join(pack_dir, "*.fasta_packed.bin"))):
            if subprocess.run(["./openzl/zli", "compress", part_file, "--compressor", model_path, "--output", f"{part_file}.zl", "--force"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0:
                raise RuntimeError("OpenZL Engine Crash")
        zl_time = round(time.time() - start_t, 2)
        
        # 2. DECOMPRESS & VALIDATE
        dec_start_time = time.time()
        is_valid = True
        for part_file in sorted(glob.glob(os.path.join(pack_dir, "*.fasta_packed.bin"))):
            zl_path = f"{part_file}.zl"
            dec_path = f"{part_file}.dec"
            if subprocess.run(["./openzl/zli", "decompress", zl_path, "--output", dec_path, "--force"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL).returncode != 0:
                raise RuntimeError("OpenZL Decompression Crash")
            if not filecmp.cmp(part_file, dec_path, shallow=False):
                is_valid = False
                
        dec_time = round(time.time() - dec_start_time, 2)
        
        zl_total_size = sum(get_file_size(f) for f in glob.glob(os.path.join(pack_dir, "*.zl")))
        results.update({
            "OpenZL_Ratio": round(orig_size / zl_total_size, 2) if zl_total_size > 0 else 0, 
            "OpenZL_Comp_sec": zl_time,
            "OpenZL_Decomp_sec": dec_time,
            "OpenZL_Valid": "PASS" if is_valid else "FAIL"
        })
        
    except Exception as e:
        print(f"     [!] Pipeline failure: {str(e)}")
        fallback_ratio, fallback_comp, fallback_dec, fallback_valid = run_fallback_zstd(fasta_path, threads)
        results.update({"OpenZL_Ratio": f"FALLBACK_ZSTD ({fallback_ratio}x)", "OpenZL_Comp_sec": fallback_comp, "OpenZL_Decomp_sec": fallback_dec, "OpenZL_Valid": "PASS" if fallback_valid else "FAIL"})

    shutil.rmtree(pack_dir, ignore_errors=True)
    return results

# ==========================================
# MAIN DISPATCHER
# ==========================================
def main():
    parser = argparse.ArgumentParser(description="Unified Genomics Benchmarking Pipeline")
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-t", "--type", choices=['fastq', 'vcf', 'fasta', 'auto'], default='auto')
    parser.add_argument("-o", "--output", default="results/unified_benchmarks.csv")
    parser.add_argument("-p", "--threads", default=16, type=int)
    parser.add_argument("--benchmark", action="store_true", help="Run external baselines")
    args = parser.parse_args()

    files_to_process = glob.glob(os.path.join(args.input, "*.*")) if os.path.isdir(args.input) else [args.input]
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    
    completed_files = set()
    results = []
    
    if os.path.exists(args.output):
        with open(args.output, 'r', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                results.append(row)
                if 'File' in row:
                    completed_files.add(row['File'])

    for filepath in files_to_process:
        base_name = os.path.basename(filepath)
        if base_name in completed_files: continue
            
        file_type = args.type
        if file_type == 'auto':
            if filepath.lower().endswith(('.fq', '.fastq', '.fastq.gz')): file_type = 'fastq'
            elif filepath.lower().endswith(('.vcf', '.vcf.gz')): file_type = 'vcf'
            elif filepath.lower().endswith(('.fna', '.fasta', '.fa')): file_type = 'fasta'
            else: continue 

        res = None
        if file_type == 'fastq': res = profile_fastq(filepath, args.threads, args.benchmark)
        elif file_type == 'vcf': res = profile_vcf(filepath, args.threads, args.benchmark)
        elif file_type == 'fasta': res = profile_fasta(filepath, args.threads, args.benchmark)
            
        if res is not None:
            results.append(res)
            
            fieldnames = []
            for r in results:
                for k in r.keys():
                    if k not in fieldnames:
                        fieldnames.append(k)
                        
            with open(args.output, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)
                
            print(f"     --> Finished {base_name}. Results saved to {args.output}")

if __name__ == "__main__":
    main()
