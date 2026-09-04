def find_vcf_anomalies(filepath, expected_columns=59):
    print(f"Scanning {filepath} for tab-delimiter anomalies...")
    error_count = 0
    
    with open(filepath, 'r') as f:
        for line_num, line in enumerate(f, 1):
            # Skip header lines
            if line.startswith('#'):
                continue
                
            # Split the line by tabs and check the length
            columns = line.strip('\n').split('\t')
            actual_columns = len(columns)
            
            if actual_columns != expected_columns:
                print("-" * 50)
                print(f"Anomaly detected at Row {line_num}!")
                print(f"Expected: {expected_columns} | Found: {actual_columns}")
                print(f"Row Preview: {line[:80]}...") # Print first 80 chars
                error_count += 1
                
                # Stop after finding the first 5 errors to avoid flooding the terminal
                if error_count >= 5:
                    print("\nStopping after 5 anomalies. Please fix the source data.")
                    return

    if error_count == 0:
        print("Data is clean! All rows match the expected schema.")

if __name__ == "__main__":
    # Point this to your raw source VCF
    find_vcf_anomalies("data/test/cohort_59col.vcf")