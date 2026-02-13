import json
import argparse
import sys
from collections import defaultdict

def infer_type(val):
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "string"
    return "string" # Fallback

def main():
    parser = argparse.ArgumentParser(description="Scan GeoJSON properties and generate SDDL/Mapping.")
    parser.add_argument("--in", dest="input", required=True, help="Input GeoJSON file")
    parser.add_argument("--out-sddl", required=True, help="Output SDDL file")
    parser.add_argument("--out-map", required=True, help="Output Mapping JSON file")
    parser.add_argument("--sample", type=int, default=1000, help="Number of records to scan")
    args = parser.parse_args()

    print(f"Scanning {args.input}...")
    
    properties_schema = {} # key -> type
    
    try:
        with open(args.input, 'r') as f:
            data = json.load(f)
            
        features = data.get('features', [])
        print(f"Found {len(features)} features. Scanning first {args.sample}...")
        
        for i, feat in enumerate(features):
            if i >= args.sample:
                break
            
            props = feat.get('properties', {})
            if not props: continue
            
            for k, v in props.items():
                if v is None: continue
                curr_type = infer_type(v)
                
                if k not in properties_schema:
                    properties_schema[k] = curr_type
                else:
                    existing = properties_schema[k]
                    if existing != curr_type:
                        properties_schema[k] = "string"
                        
    except Exception as e:
        print(f"Error reading JSON: {e}")
        sys.exit(1)

    print(f"Detected {len(properties_schema)} property fields:")
    sorted_keys = sorted(properties_schema.keys())
    for k in sorted_keys:
        print(f"  - {k}: {properties_schema[k]}")

    mapping = {
        "fields": sorted_keys
    }
    
    with open(args.out_map, 'w') as f:
        json.dump(mapping, f, indent=2)
    print(f"Wrote mapping to {args.out_map}")

    # --- Generate SDDL ---
    sddl_lines = []
    sddl_lines.append("U32 = UInt32LE")
    sddl_lines.append("F64 = Float64LE")
    sddl_lines.append("")
    sddl_lines.append("# Universal GeoJSON Packed Schema")
    sddl_lines.append("# Magic \"GEO1\"")
    sddl_lines.append("")
    sddl_lines.append("magic: Byte[4]")
    sddl_lines.append("num_features: U32")
    sddl_lines.append("")
    
    # Offsets
    sddl_lines.append("# --- Offsets ---")
    for k in sorted_keys:
        clean_k = k.replace(" ", "_").replace("-", "_").replace(".", "_")
        sddl_lines.append(f"off_{clean_k}: U32[num_features + 1]")
    sddl_lines.append("off_geom: U32[num_features + 1]")
    sddl_lines.append("")
    
    # Lengths
    sddl_lines.append("# --- Lengths ---")
    for k in sorted_keys:
        clean_k = k.replace(" ", "_").replace("-", "_").replace(".", "_")
        sddl_lines.append(f"len_{clean_k}: U32")
    sddl_lines.append("total_points: U32")
    sddl_lines.append("")
    
    # Padding
    sddl_lines.append("# --- Padding (8-byte align) ---")
    for k in sorted_keys:
        clean_k = k.replace(" ", "_").replace("-", "_").replace(".", "_")
        sddl_lines.append(f"pad_{clean_k}: U32")
    
    if len(sorted_keys) % 2 != 0:
        sddl_lines.append("pad_align: U32")

    sddl_lines.append("")
    
    # Data
    sddl_lines.append("# --- Data Columns ---")
    for k in sorted_keys:
        clean_k = k.replace(" ", "_").replace("-", "_").replace(".", "_")
        sddl_lines.append(f"dat_{clean_k}: Byte[len_{clean_k} + pad_{clean_k}]")
    sddl_lines.append("")
    
    # Geometry
    sddl_lines.append("# --- Geometry ---")
    sddl_lines.append("coords_x: F64[total_points]")
    sddl_lines.append("coords_y: F64[total_points]")
    sddl_lines.append("coords_z: F64[total_points]")
    
    with open(args.out_sddl, 'w') as f:
        f.write("\n".join(sddl_lines))
        f.write("\n") # Ensure trailing newline
    print(f"Wrote SDDL to {args.out_sddl}")

if __name__ == "__main__":
    main()
