import os
import subprocess
import pandas as pd
import time
import glob
import argparse

def get_file_size(filepath):
    if filepath and os.path.exists(filepath):
        return os.path.getsize(filepath)
    return 0

def run_with_metrics(cmd_list, output_file=None):
    """Executes a command, tracks wall time, and extracts Max RSS memory via /usr/bin/time."""
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

def main():
    parser = argparse.ArgumentParser(description="Standalone Genozip Benchmarking Pipeline")
    parser.add_argument("-i", "--input", required=True, help="Path to file or directory of VCFs/FASTQs")
    parser.add_argument("-o", "--output", default="results/genozip_benchmarks.csv")
    args = parser.parse_args()

    files_to_process = glob.glob(os.path.join(args.input, "*.*")) if os.path.isdir(args.input) else [args.input]
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    
    results = []
    
    for filepath in files_to_process:
        base_name = os.path.basename(filepath)
        orig_size = get_file_size(filepath)
        
        # Skip unsupported file extensions automatically
        if not filepath.lower().endswith(('.fastq', '.fq', '.vcf')):
            continue
            
        print(f"\nProfiling Genozip on: {base_name}")
        
        out_comp = f"{filepath}.genozip"
        out_dec = f"{filepath}.dec"
        
        # 1. Compress
        print("  -> Compressing...")
        comp_cmd = ["genozip", filepath, "--force", "-o", out_comp]
        comp_size, comp_time, comp_mem, c_ret = run_with_metrics(comp_cmd, out_comp)
        
        ratio = round(orig_size / comp_size, 2) if comp_size > 0 and c_ret == 0 else 0
        
        # 2. Decompress
        decomp_time, decomp_mem = 0.0, 0.0
        if c_ret == 0:
            print("  -> Decompressing...")
            decomp_cmd = ["genounzip", out_comp, "--force", "-o", out_dec]
            _, decomp_time, decomp_mem, _ = run_with_metrics(decomp_cmd, out_dec)
        else:
            print("  -> [!] Compression failed.")

        # Cleanup
        if os.path.exists(out_comp): os.remove(out_comp)
        if os.path.exists(out_dec): os.remove(out_dec)
        
        # Append Results
        results.append({
            "File": base_name,
            "Original_MB": round(orig_size / (1024 * 1024), 2),
            "Genozip_Ratio": ratio if c_ret == 0 else "FAIL",
            "Genozip_Comp_sec": comp_time,
            "Genozip_Decomp_sec": decomp_time,
            "Genozip_Mem_MB": max(comp_mem, decomp_mem)
        })
        
        # Save incrementally
        pd.DataFrame(results).to_csv(args.output, index=False)
        print(f"  -> Saved metrics for {base_name}.")

    print(f"\nGenozip Benchmark Complete! Results saved to {args.output}")

if __name__ == "__main__":
    main()
