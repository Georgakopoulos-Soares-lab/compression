import os
import subprocess
import pandas as pd
import time
import glob
import shutil

def get_file_size(filepath):
    if filepath and os.path.exists(filepath):
        return os.path.getsize(filepath)
    return 0

def run_command(cmd_args, output_file=None):
    start_time = time.time()
    subprocess.run(cmd_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end_time = time.time()
    size = get_file_size(output_file) if output_file else 0
    return size, round(end_time - start_time, 2)

def get_vcf_column_count(filepath):
    """Scans the VCF to detect the exact schema width (number of columns)."""
    with open(filepath, 'rt') as f:
        for line in f:
            if not line.startswith('#'):
                # ONLY strip the newline, preserve trailing tabs if they exist
                return len(line.rstrip('\n').split('\t'))
    return 0

def profile_vcf(vcf_path):
    print(f"\nProfiling: {vcf_path}...")
    orig_size = get_file_size(vcf_path)
    base_name = os.path.basename(vcf_path)
    
    # Detect the schema width for our ML model
    num_cols = get_vcf_column_count(vcf_path)
    print(f"  -> Detected VCF Schema: {num_cols} total columns")
    
    # ==========================================
    # PHASE 1: RAW VCF COMPRESSION
    # ==========================================
    print("  -> Testing Raw VCF with ZSTD...")
    zstd_vcf_out = f"{vcf_path}.zst"
    zstd_vcf_size, zstd_vcf_time = run_command(["zstd", "-19", "-k", "-f", vcf_path], zstd_vcf_out)
    if os.path.exists(zstd_vcf_out):
        os.remove(zstd_vcf_out) 
    
    # ==========================================
    # PHASE 2: PRE-PROCESSING (Uncompressed BCF)
    # ==========================================
    print("  -> Pre-processing to uncompressed BCF...")
    bcf_path = f"{vcf_path}.bcf"
    run_command(["./bcftools-1.19/bcftools", "view", "-O", "u", "-o", bcf_path, vcf_path])
    bcf_size = get_file_size(bcf_path)
    
    # ==========================================
    # PHASE 3: PRE-PROCESSED COMPRESSION
    # ==========================================
    print("  -> Testing Pre-Processed BCF with ZSTD...")
    zstd_bcf_out = f"{bcf_path}.zst"
    zstd_bcf_size, zstd_bcf_time = run_command(["zstd", "-19", "-k", "-f", bcf_path], zstd_bcf_out)
    
    if os.path.exists(zstd_bcf_out):
        os.remove(zstd_bcf_out)
    if os.path.exists(bcf_path):
        os.remove(bcf_path)
        
    # ==========================================
    # PHASE 4: OpenZL / NYX COMPRESSION 
    # ==========================================
    print("  -> Executing OpenZL Framework...")
    pack_dir = f"{vcf_path}_pack"
    os.makedirs(pack_dir, exist_ok=True)
    
    # 1. Split the file into optimized 40MB chunks
    print("     Splitting Header and Chunking Body (40 MiB)...")
    subprocess.run([
        "./tools/vcf_preprocessing", vcf_path, pack_dir, 
        "--threads", "16", "--max-chunk-mib", "40", "--force"
    ], stdout=subprocess.DEVNULL)
    
    # ==========================================
    # DYNAMIC MODEL GATE (Schema-Aware Training)
    # ==========================================
    os.makedirs("artifacts", exist_ok=True)
    model_path = f"artifacts/model_{num_cols}_cols.zlc"
    body_parts_dir = os.path.join(pack_dir, "body_parts")
    
    if not os.path.exists(model_path):
        print(f"Schema mismatch: No cached model found for {num_cols} columns.")
        print(f"Auto-training custom model dynamically...")
        
        # Setup temporary training environment
        train_tmp_dir = os.path.join(pack_dir, "train_tmp")
        os.makedirs(train_tmp_dir, exist_ok=True)
        
        # DYNAMIC TRAINING SAMPLE SELECTION
        parts = sorted([f for f in os.listdir(body_parts_dir) if f.endswith(".vcfbody")])
        if parts:
            total_chunks = len(parts)
            
            # Guarantee a safe floor of 10 chunks (400 MiB) to prevent strict type-inference crashes.
            # Scale up to 25 chunks for massive datasets. If the file has <10 chunks, take them all.
            sample_size = min(total_chunks, max(10, int(total_chunks * 0.10)))
            
            print(f"Dynamic training: Sampling {sample_size} out of {total_chunks} chunks.")
            
            chunks_to_train = parts[:sample_size]
            for chunk in chunks_to_train:
                shutil.copy(os.path.join(body_parts_dir, chunk), train_tmp_dir)
            
            # Execute the training sequence
            subprocess.run([
                "./openzl/zli", "train", train_tmp_dir,
                "--profile", "csv", "--profile-arg", "\t",
                "--output", model_path,
                "--force", "--threads", "16", "--use-all-samples"
            ], stdout=subprocess.DEVNULL)
            
            print(f"Robust custom model generated successfully: {model_path}")
            shutil.rmtree(train_tmp_dir)
        else:
            print("ERROR: No body parts found for training.")
            return None
    else:
        print(f"Verified cached schema model: {model_path}")

    # ==========================================
    # 2. Compress chunks with the designated model
    # ==========================================
    print("     Compressing chunks...")
    zl_dir = os.path.join(pack_dir, "zl")
    os.makedirs(zl_dir, exist_ok=True)
    
    start_time = time.time()
    
    if os.path.exists(body_parts_dir):
        parts = sorted([f for f in os.listdir(body_parts_dir) if f.endswith(".vcfbody")])
        for part_file in parts:
            in_path = os.path.join(body_parts_dir, part_file)
            out_path = os.path.join(zl_dir, f"{part_file}.zl")
            
            res = subprocess.run([
                "./openzl/zli", "compress", in_path,
                "--compressor", model_path,
                "--output", out_path, "--force"
            ], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
            
            if res.returncode != 0:
                print(f"FAILED at {part_file}. Check raw data at {in_path}")
                error_msg = res.stderr.decode('utf-8', errors='ignore')
                print(f"C-ENGINE ERROR:\n{error_msg}")
                return None
                
    end_time = time.time()
    zl_time = round(end_time - start_time, 2)
    
    # Calculate total size
    zl_total_size = 0
    header_path = os.path.join(pack_dir, "header.vcf")
    zl_total_size += get_file_size(header_path)
    if os.path.exists(zl_dir):
        for zl_file in os.listdir(zl_dir):
            zl_total_size += get_file_size(os.path.join(zl_dir, zl_file))
            
    subprocess.run(["rm", "-rf", pack_dir])
    
    zstd_raw_ratio = orig_size / zstd_vcf_size if zstd_vcf_size > 0 else 0
    zstd_preproc_ratio = orig_size / zstd_bcf_size if zstd_bcf_size > 0 else 0
    zl_ratio = orig_size / zl_total_size if zl_total_size > 0 else 0
    
    return {
        "File": base_name,
        "Columns": num_cols,
        "Original_MB": round(orig_size / (1024*1024), 2),
        "ZSTD_Ratio": round(zstd_raw_ratio, 2),
        "BCF_ZSTD_Ratio": round(zstd_preproc_ratio, 2),
        "OpenZL_Ratio": round(zl_ratio, 2),
        "OpenZL_Time_sec": zl_time
    }

def main():
    # Target our specific GIAB out-of-distribution file
    search_path = "data/test/*.vcf"
    vcf_files = glob.glob(search_path)
    
    if not vcf_files:
        print(f"No valid VCF files found for '{search_path}'.")
        return
        
    print(f"Found {len(vcf_files)} VCF files. Initiating dynamic run...")
    
    results = []
    for vcf in vcf_files:
        res = profile_vcf(vcf)
        results.append(res)
            
    if results:
        valid_results = [res for res in results if res is not None]
        if valid_results:
            df = pd.DataFrame(valid_results)
            os.makedirs("results", exist_ok=True)
            csv_filename = "results/vcf_giab_compression_results.csv"
            df.to_csv(csv_filename, index=False)
            print(f"\nDynamic Profiling complete! Results saved to {csv_filename}")
            print(df.to_string())

if __name__ == "__main__":
    main()