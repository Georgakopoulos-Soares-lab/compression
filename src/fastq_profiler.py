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

def get_fastq_properties(filepath):
    """Scans the FASTQ to detect read length and estimate structural properties."""
    with open(filepath, 'rt') as f:
        f.readline() # Skip header
        seq = f.readline().strip()
        return len(seq)

def sort_fastq_lexicographically(fastq_path, output_dir):
    """
    Reads a FASTQ file, sorts the reads lexicographically by sequence, 
    and writes out a sorted FASTQ for maximum downstream entropy compression.
    """
    print(f"  -> [NYX Architecture] Sorting FASTQ: {os.path.basename(fastq_path)}")
    os.makedirs(output_dir, exist_ok=True)
    sorted_fastq_path = os.path.join(output_dir, f"sorted_{os.path.basename(fastq_path)}")

    start_time = time.time()
    records = []
    
    # 1. Load the entire FASTQ sample into memory as grouped records
    print("     Loading and parsing records...")
    with open(fastq_path, 'r') as f_in:
        while True:
            header = f_in.readline()
            if not header:
                break  # EOF
            sequence = f_in.readline()
            plus = f_in.readline()
            quality = f_in.readline()
            
            # Tuple structure: (sequence, header, plus, quality)
            records.append((sequence, header, plus, quality))

    # 2. Sort the records alphabetically based on the sequence
    print(f"     Sorting {len(records)} reads lexicographically...")
    records.sort(key=lambda x: x[0])
    
    # 3. Write out the sorted FASTQ format
    print("     Writing sorted FASTQ stream...")
    with open(sorted_fastq_path, 'w') as f_out:
        for seq, head, plus, qual in records:
            f_out.write(head)
            f_out.write(seq)
            f_out.write(plus)
            f_out.write(qual)

    elapsed = time.time() - start_time
    print(f"     Sorting complete in {elapsed:.2f} seconds.")
    
    return sorted_fastq_path


def profile_fastq(fastq_path):
    print(f"\nProfiling: {fastq_path}...")
    orig_size = get_file_size(fastq_path)
    base_name = os.path.basename(fastq_path)
    
    # Detect structural properties
    read_length = get_fastq_properties(fastq_path)
    print(f"  -> Detected FASTQ Read Length: {read_length} bp")
    
    # ==========================================
    # PHASE 1: RAW FASTQ COMPRESSION (Baseline)
    # ==========================================
    print("  -> Testing Raw FASTQ with ZSTD...")
    zstd_raw_out = f"{fastq_path}.zst"
    zstd_raw_size, zstd_raw_time = run_command(["zstd", "-19", "-k", "-f", fastq_path], zstd_raw_out)
    if os.path.exists(zstd_raw_out):
        os.remove(zstd_raw_out) 
        
    # ==========================================
    # PHASE 1.5: IN-MEMORY LEXICOGRAPHICAL SORT
    # ==========================================
    sort_dir = f"{fastq_path}_sorted_tmp"
    sorted_fastq_path = sort_fastq_lexicographically(fastq_path, sort_dir)

    # ==========================================
    # PHASE 2: C++ Preprocessing & TSV Chunking
    # ==========================================
    print("  -> Executing format-aware C++ preprocessor on sorted reads...")
    pack_dir = f"{fastq_path}_pack"
    os.makedirs(pack_dir, exist_ok=True)
    pp_prefix = os.path.join(pack_dir, base_name)
    
    # Route the sorted FASTQ through the C++ encode tool to get the TSV and Meta sidecar
    res = subprocess.run([
        "./tools/fastq_preprocess", "encode", sorted_fastq_path, pp_prefix, "16"
    ], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    
    if res.returncode != 0:
        print(f"FAILED: Preprocessor error.\n{res.stderr.decode('utf-8')}")
        # Clean up sort dir before exiting
        shutil.rmtree(sort_dir, ignore_errors=True)
        return None

    print("  -> Chunking TSV output...")
    body_parts_dir = os.path.join(pack_dir, "body_parts")
    os.makedirs(body_parts_dir, exist_ok=True)
    
    tsv_file = f"{pp_prefix}.tsv"
    if os.path.exists(tsv_file):
        subprocess.run([
            "split", "-l", "500000", "-d", "--additional-suffix=.tsv", 
            tsv_file, os.path.join(body_parts_dir, "part_")
        ])
        os.remove(tsv_file) 
    else:
        print("ERROR: Expected TSV not found.")
        shutil.rmtree(sort_dir, ignore_errors=True)
        return None

    # ==========================================
    # PHASE 3: DYNAMIC MODEL GATE (Distributed Sampling)
    # ==========================================
    os.makedirs("artifacts", exist_ok=True)
    model_path = f"artifacts/model_{base_name}.zlc"
    
    if not os.path.exists(model_path):
        print(f"  -> Auto-training robust model on distributed TSV segments...")
        train_tmp_dir = os.path.join(pack_dir, "train_tmp")
        os.makedirs(train_tmp_dir, exist_ok=True)
        
        parts = sorted([f for f in os.listdir(body_parts_dir) if f.endswith(".tsv")])
        
        # INCREASED ROBUSTNESS: Sample ~5 evenly distributed chunks across the file
        step = max(1, len(parts) // 5)
        for idx in range(0, len(parts), step):
            shutil.copy(os.path.join(body_parts_dir, parts[idx]), train_tmp_dir)
        
        subprocess.run([
            "./openzl/zli", "train", train_tmp_dir,
            "--profile", "csv", "--profile-arg", "\t",
            "--output", model_path,
            "--force", "--threads", "16", "--use-all-samples"
        ], stdout=subprocess.DEVNULL)
    else:
        print(f"  -> Verified cached model: {model_path}")

    # ==========================================
    # PHASE 4: OpenZL COMPRESSION 
    # ==========================================
    print("  -> Compressing TSV chunks...")
    zl_dir = os.path.join(pack_dir, "zl")
    os.makedirs(zl_dir, exist_ok=True)
    
    start_t = time.time()
    for part_file in sorted(os.listdir(body_parts_dir)):
        in_path = os.path.join(body_parts_dir, part_file)
        out_zl = os.path.join(zl_dir, f"{part_file}.zl")
        
        res = subprocess.run([
            "./openzl/zli", "compress", in_path,
            "--compressor", model_path,
            "--output", out_zl, "--force"
        ], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        
        if res.returncode != 0:
            print(f"C-ENGINE ERROR on {part_file}:\n{res.stderr.decode('utf-8')}")
            shutil.rmtree(sort_dir, ignore_errors=True)
            return None
            
    zl_total_time = time.time() - start_t
    
    # Calculate total size (Meta Sidecar + ZL parts)
    zl_total_size = get_file_size(f"{pp_prefix}.meta")
    for zl_file in os.listdir(zl_dir):
        zl_total_size += get_file_size(os.path.join(zl_dir, zl_file))
        
    # Cleanup temporary directories
    subprocess.run(["rm", "-rf", pack_dir])
    shutil.rmtree(sort_dir, ignore_errors=True)
    
    zstd_raw_ratio = orig_size / zstd_raw_size if zstd_raw_size > 0 else 0
    zl_ratio = orig_size / zl_total_size if zl_total_size > 0 else 0
    
    return {
        "File": base_name,
        "Read_Length": read_length,
        "Original_MB": round(orig_size / (1024*1024), 2),
        "ZSTD_Raw_Ratio": round(zstd_raw_ratio, 2),
        "OpenZL_Ratio": round(zl_ratio, 2),
        "OpenZL_Time_sec": round(zl_total_time, 2)
    }


def get_completed_files(csv_path):
    """Reads the results CSV and returns a set of already processed filenames."""
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path)
            if 'File' in df.columns:
                return set(df['File'].tolist())
        except Exception as e:
            print(f"Warning: Could not read existing ledger {csv_path}. Error: {e}")
    return set()


def main():
    search_path = "data/fastq/*.fastq"
    fastq_files = glob.glob(search_path)
    
    if not fastq_files:
        print(f"No valid FASTQ files found for '{search_path}'.")
        return
        
    print(f"Found {len(fastq_files)} FASTQ files. Initiating dynamic profiling...")
    
    # 1. Define the ledger path and load existing completions
    os.makedirs("results", exist_ok=True)
    csv_filename = "results/fastq_graph_compression_results.csv"
    completed_files = get_completed_files(csv_filename)
    
    if completed_files:
        print(f"  -> Found {len(completed_files)} previously completed files in ledger.")
    
    # Load existing results into our list so we append rather than overwrite
    results = []
    if os.path.exists(csv_filename):
        existing_df = pd.read_csv(csv_filename)
        results = existing_df.to_dict('records')

    new_runs = 0
    failed_runs = 0 # Track failures explicitly
    
    for fastq in fastq_files:
        base_name = os.path.basename(fastq)
        
        if base_name in completed_files:
            print(f"\nSkipping: {base_name} (Already present in results CSV).")
            continue
            
        res = profile_fastq(fastq)
        if res is not None:
            results.append(res)
            new_runs += 1
            df = pd.DataFrame(results)
            df.to_csv(csv_filename, index=False)
        else:
            failed_runs += 1 # Increment on crash
            
    # Final Summary Logic
    if new_runs > 0:
        df = pd.DataFrame(results)
        print(f"\nDynamic Profiling complete! {new_runs} new files processed.")
        print(f"Ledger updated: {csv_filename}")
        print("\nNew Additions:")
        print(df.tail(new_runs).to_string(index=False))
    elif failed_runs > 0:
        print(f"\nPipeline finished, but {failed_runs} files FAILED due to C-engine errors.")
    else:
        print("\nAll files were already processed. No new profiling required.")

if __name__ == "__main__":
    main()
