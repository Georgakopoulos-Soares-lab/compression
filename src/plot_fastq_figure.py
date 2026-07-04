import os
import csv
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ==========================================
# 1. NATURE JOURNAL STYLE CONFIGURATION
# ==========================================
plt.rcParams.update({
    'font.family': 'sans-serif',
    'font.sans-serif': ['Arial', 'Helvetica', 'DejaVu Sans'],
    'font.size': 8,
    'axes.labelsize': 8,
    'axes.titlesize': 9,
    'xtick.labelsize': 7,
    'ytick.labelsize': 7,
    'legend.fontsize': 7,
    'figure.figsize': (7.2, 2.8), 
    'figure.dpi': 300
})

# ==========================================
# 2. WIDE-FORMAT CSV PARSER
# ==========================================
def extract_data(filepath):
    data = {}
    if not os.path.exists(filepath):
        return data
        
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if 'Original_MB' in row and row['Original_MB']:
                data['Original_MB'] = float(row['Original_MB'])
                
            mapping = {
                'OpenZL': 'OpenZL', 
                'Genozip': 'Genozip', 
                'XZ': 'xz', 
                'ZSTD': 'ZSTD', 
                'GZIP': 'pigz',
                'SPRING': 'SPRING'  # Added SPRING for FASTQ benchmarks
            }
            
            for csv_prefix, plot_name in mapping.items():
                r_col = f"{csv_prefix}_Ratio"
                c_col = f"{csv_prefix}_Comp_sec"
                d_col = f"{csv_prefix}_Decomp_sec"
                
                if r_col in row and row[r_col].strip():
                    try:
                        ratio_str = row[r_col].replace('x', '').replace('×', '').strip()
                        # Handle tool failures gracefully
                        if ratio_str == "FAIL" or "FALLBACK" in ratio_str:
                            continue
                            
                        ratio = float(ratio_str)
                        comp = float(row[c_col].strip()) if c_col in row and row[c_col].strip() else 0.0
                        decomp = float(row[d_col].strip()) if d_col in row and row[d_col].strip() else 0.0
                        
                        data[plot_name] = {'Ratio': ratio, 'CompTime': comp, 'DecompTime': decomp}
                    except ValueError:
                        continue
            break
    return data

# ==========================================
# 3. LOAD ALL FASTQ BENCHMARK DATA
# ==========================================
results_dir = 'results'

# Panel A & B data targets
srr_data = extract_data(f'{results_dir}/SRR_baseline.csv') # Fixed length
err_data = extract_data(f'{results_dir}/ERR_baseline.csv') # Variable length

# Panel C Scalability targets
cores = [1, 2, 4, 8, 16]
openzl_times = []
genozip_times = []

for c in cores:
    scale_data = extract_data(f'{results_dir}/ERR_scale_{c}t.csv')
    openzl_times.append(scale_data.get('OpenZL', {}).get('CompTime', 0))
    genozip_times.append(scale_data.get('Genozip', {}).get('CompTime', 0))

print("\n--- FASTQ Core Scaling Data Status ---")
print(f"{'Cores':<8}{'OpenZL Time (s)':<18}{'Genozip Time (s)':<18}{'Status'}")
for i, c in enumerate(cores):
    ozl = openzl_times[i]
    gz = genozip_times[i]
    status = "READY" if (ozl > 0 and gz > 0) else "PENDING/MISSING"
    print(f"{c:<8}{ozl:<18.2f}{gz:<18.2f}{status}")
print("--------------------------------------\n")

# ==========================================
# 4. PLOT GENERATION
# ==========================================
colors = {'OpenZL': '#D55E00', 'Genozip': '#0072B2', 'SPRING': '#56B4E9', 
          'xz': '#009E73', 'ZSTD': '#CC79A7', 'pigz': '#F0E442'}
tools = ['OpenZL', 'Genozip', 'SPRING', 'xz', 'ZSTD', 'pigz']

fig, axs = plt.subplots(1, 3, constrained_layout=True)

for ax, label in zip(axs, ['a', 'b', 'c']):
    ax.text(-0.1, 1.05, label, transform=ax.transAxes, 
            fontsize=10, fontweight='bold', va='top', ha='right')

# --- PANEL A: Compression Ratios ---
for tool in tools:
    if tool in srr_data and tool in err_data:
        axs[0].scatter(srr_data[tool]['Ratio'], err_data[tool]['Ratio'], 
                       label=tool, color=colors[tool], s=50, zorder=3)

axs[0].set_xlabel('Compression Ratio (Fixed-Length SRR)')
axs[0].set_ylabel('Compression Ratio (Variable-Length ERR)')
axs[0].grid(True, linestyle='--', alpha=0.5, zorder=0)
axs[0].legend(frameon=False)

# --- PANEL B: Compression vs Decompression Rates ---
FILE_SIZE_MB = err_data.get('Original_MB', 7990.0) # Fallback to ~7.99 GB

for tool in tools:
    if tool in err_data and err_data[tool].get('CompTime', 0) > 0 and err_data[tool].get('DecompTime', 0) > 0:
        comp_rate = FILE_SIZE_MB / err_data[tool]['CompTime']
        decomp_rate = FILE_SIZE_MB / err_data[tool]['DecompTime']
        axs[1].scatter(comp_rate, decomp_rate, color=colors[tool], marker='o', s=40, alpha=0.8)

axs[1].set_xlabel('Compression rate (MB/s)')
axs[1].set_ylabel('Decompression rate (MB/s)')
axs[1].set_xscale('log')
axs[1].set_yscale('log')
axs[1].grid(True, linestyle='--', alpha=0.5)

legend_elements = [mpatches.Patch(color='none', label='Dataset:'),
                   plt.Line2D([0], [0], marker='o', color='w', markerfacecolor='k', markersize=6, label='ERR9539086')]
axs[1].legend(handles=legend_elements, frameon=False, loc='lower right')

# --- PANEL C: Scalability ---
if all(t > 0 for t in openzl_times) and all(t > 0 for t in genozip_times) and len(openzl_times) == len(cores):
    openzl_speedup = [openzl_times[0] / t for t in openzl_times]
    genozip_speedup = [genozip_times[0] / t for t in genozip_times]

    axs[2].plot(cores, openzl_speedup, marker='o', color=colors['OpenZL'], label='OpenZL')
    axs[2].plot(cores, genozip_speedup, marker='s', color=colors['Genozip'], label='Genozip')
    axs[2].plot(cores, cores, 'k--', alpha=0.5, label='Ideal scaling')
    axs[2].legend(frameon=False)
else:
    axs[2].text(0.5, 0.5, 'Scaling Data\nMissing/In Progress', ha='center', va='center', alpha=0.5)

axs[2].set_xlabel('Number of CPU cores')
axs[2].set_ylabel('Speedup')
axs[2].set_xticks(cores)
axs[2].grid(True, linestyle='--', alpha=0.5)

# ==========================================
# 5. SAVE ALL PLOTS
# ==========================================
os.makedirs('artifacts', exist_ok=True)

plt.savefig('artifacts/fastq_nature_benchmark_combined.pdf', format='pdf', bbox_inches='tight', dpi=300)

extent_a = axs[0].get_window_extent().transformed(fig.dpi_scale_trans.inverted())
fig.savefig('artifacts/fastq_panel_a_ratios.pdf', bbox_inches=extent_a.expanded(1.2, 1.2))

extent_b = axs[1].get_window_extent().transformed(fig.dpi_scale_trans.inverted())
fig.savefig('artifacts/fastq_panel_b_rates.pdf', bbox_inches=extent_b.expanded(1.2, 1.2))

extent_c = axs[2].get_window_extent().transformed(fig.dpi_scale_trans.inverted())
fig.savefig('artifacts/fastq_panel_c_scalability.pdf', bbox_inches=extent_c.expanded(1.2, 1.2))

print("Successfully saved FASTQ combined figure AND individual plots to artifacts/ directory!")
