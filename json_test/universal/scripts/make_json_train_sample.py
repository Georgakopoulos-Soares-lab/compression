import json
import sys
import argparse

def main():
    parser = argparse.ArgumentParser(description="Create a training sample from a GeoJSON FeatureCollection.")
    parser.add_argument("--in", dest="input", required=True, help="Input JSON file")
    parser.add_argument("--out", dest="output", required=True, help="Output JSON file")
    parser.add_argument("--target-mib", type=int, default=200, help="Target size in MiB")
    args = parser.parse_args()

    target_bytes = args.target_mib * 1024 * 1024
    
    print(f"Reading {args.input}...")
    # We use ijson or just read line by line if it's newline delimited, but citylots is standard JSON.
    # For simplicity, we'll load it all (180MB is fine) or stream it if we want to be fancy.
    # Given the file size, loading is fine.
    
    with open(args.input, 'r') as f:
        data = json.load(f)
    
    if 'features' not in data:
        print("Error: No 'features' key in JSON")
        sys.exit(1)
        
    features = data['features']
    total_features = len(features)
    print(f"Total features: {total_features}")
    
    # Estimate size per feature roughly
    # We can just serialize features one by one until we hit the limit.
    
    sample_features = []
    current_size = 0
    
    # Base overhead
    dummy = {k:v for k,v in data.items() if k != 'features'}
    dummy['features'] = []
    base_size = len(json.dumps(dummy))
    current_size = base_size
    
    for feat in features:
        # This is slow but accurate enough
        feat_str = json.dumps(feat)
        feat_size = len(feat_str)
        
        if current_size + feat_size > target_bytes and len(sample_features) > 0:
            break
            
        sample_features.append(feat)
        current_size += feat_size
        
        # Account for comma
        current_size += 1
        
    print(f"Selected {len(sample_features)} features.")
    
    out_data = {k:v for k,v in data.items() if k != 'features'}
    out_data['features'] = sample_features
    
    print(f"Writing {args.output}...")
    with open(args.output, 'w') as f:
        json.dump(out_data, f)
        
    print("Done.")

if __name__ == "__main__":
    main()
