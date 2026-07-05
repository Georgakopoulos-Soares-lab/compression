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
    """Reads a wide-format CSV and maps the specific column names."""
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
                'GZIP': 'pigz'
            }
            
            for csv_prefix, plot_name in mapping.items():
                r_col = f"{csv_prefix}_Ratio"
                c_col = f"{csv_prefix}_Comp_sec"
                d_col = f"{csv_prefix}_Decomp_sec"
                
                if r_col in row and row[r_col].strip():
                    try:
                        ratio = float(row[r_col].replace('x', '').replace('×', '').strip())
                        comp = float(row[c_col].strip()) if c_col in row and row[c_col].strip() else 0.0
                        decomp = float(row[d_col].strip()) if d_col in row and row[d_col].strip() else 0.0
                        
                        data[plot_name] = {'Ratio': ratio, 'CompTime': comp, 'DecompTime': decomp}
                    except ValueError:
                        continue
            break
    return data

# ==========================================
# 3. LOAD ALL BENCHMARK DATA
# ==========================================
results_dir = 'results'

clinvar_data = extract_data(f'{results_dir}/clinvar_baseline.csv')
kg_data = extract_data(f'{results_dir}/1000G_baseline.csv')

cores = [1, 2, 4, 8, 16]
openzl_times = []
genozip_times = []

for c in cores:
    scale_data = extract_data(f'{results_dir}/1000G_scale_{c}t.csv')
    openzl_times.append(scale_data.get('OpenZL', {}).get('CompTime', 0))
    genozip_times.append(scale_data.get('Genozip', {}).get('CompTime', 0))

# Print data status diagnostics
print("\n--- Core Scaling Data Status ---")
print(f"{'Cores':<8}{'OpenZL Time (s)':<18}{'Genozip Time (s)':<18}{'Status'}")
for i, c in enumerate(cores):
    ozl = openzl_times[i]
    gz = genozip_times[i]
    status = "READY" if (ozl > 0 and gz > 0) else "PENDING/RUNNING"
    print(f"{c:<8}{ozl:<18.2f}{gz:<18.2f}{status}")
print("--------------------------------\n")

# ==========================================
# 4. PLOT GENERATION
# ==========================================
colors = {'OpenZL': '#D55E00', 'Genozip': '#0072B2', 'xz': '#009E73', 
          'ZSTD': '#CC79A7', 'pigz': '#F0E442'}
tools = ['OpenZL', 'Genozip', 'xz', 'ZSTD', 'pigz']

fig, axs = plt.subplots(1, 3, constrained_layout=True)

for ax, label in zip(axs, ['a', 'b', 'c']):
    ax.text(-0.1, 1.05, label, transform=ax.transAxes, 
            fontsize=10, fontweight='bold', va='top', ha='right')

# --- PANEL A: Compression Ratios ---
for tool in tools:
    if tool in clinvar_data and tool in kg_data:
        axs[0].scatter(clinvar_data[tool]['Ratio'], kg_data[tool]['Ratio'], 
                       label=tool, color=colors[tool], s=50, zorder=3)

axs[0].set_xlabel('Compression Ratio (FORMAT-rich)')
axs[0].set_ylabel('Compression Ratio (Genotype-rich)')
axs[0].grid(True, linestyle='--', alpha=0.5, zorder=0)
axs[0].legend(frameon=False)

# --- PANEL B: Compression vs Decompression Rates ---
FILE_SIZE_MB = kg_data.get('Original_MB', 10690.0)

for tool in tools:
    if tool in kg_data and kg_data[tool].get('CompTime', 0) > 0 and kg_data[tool].get('DecompTime', 0) > 0:
        comp_rate = FILE_SIZE_MB / kg_data[tool]['CompTime']
        decomp_rate = FILE_SIZE_MB / kg_data[tool]['DecompTime']
        axs[1].scatter(comp_rate, decomp_rate, color=colors[tool], marker='o', s=40, alpha=0.8)

axs[1].set_xlabel('Compression rate (MB/s)')
axs[1].set_ylabel('Decompression rate (MB/s)')
axs[1].set_xscale('log')
axs[1].set_yscale('log')
axs[1].grid(True, linestyle='--', alpha=0.5)

legend_elements = [mpatches.Patch(color='none', label='Dataset:'),
                   plt.Line2D([0], [0], marker='o', color='w', markerfacecolor='k', markersize=6, label='1000G')]
axs[1].legend(handles=legend_elements, frameon=False, loc='lower right')

# --- PANEL C: Scalability ---
target_tools = ['OpenZL', 'Genozip', 'xz', 'ZSTD', 'pigz']

# Define a consistent, colorblind-friendly palette and unique markers for each tool
colors = {'OpenZL': '#D55E00', 'Genozip': '#0072B2', 'xz': '#009E73',
          'ZSTD': '#CC79A7', 'pigz': '#F0E442'}
markers = {'OpenZL': 'o', 'Genozip': 's', 'SPRING': '^', 'ZSTD': 'D', 'GZIP': 'v', 'XZ': 'x'}

threads = [1, 2, 4, 8, 16]
scale_data = {tool: [] for tool in target_tools}

# --- 1. Dynamically Load Data for All Tools ---
for t in threads:
    csv_file = f'results/1000G_scale_{t}t.csv' 
    
    if os.path.exists(csv_file):
        with open(csv_file, 'r') as f:
            row = list(csv.DictReader(f))[0]
            for tool in target_tools:
                valid_key = f'{tool}_Valid'
                if valid_key in row and row[valid_key] == 'PASS':
                    time_key = f'{tool}_Comp_sec'
                    if time_key in row and row[time_key]:
                        scale_data[tool].append(float(row[time_key]))
                    else:
                        scale_data[tool].append(None)
                else:
                    scale_data[tool].append(None)
    else:
        for tool in target_tools:
            scale_data[tool].append(None)

# --- 2. Plot All Valid Lines ---
for tool, times in scale_data.items():
    # Only draw the line if the tool has at least one successful run
    if any(t is not None for t in times):
        axs[2].plot(threads, times, marker=markers.get(tool, 'o'), color=colors.get(tool, '#333333'), 
                    label=tool, linewidth=2, markersize=6)
        
        # --- 3. Strategic Text Labels ---
        # We only annotate the main contenders to prevent graph clutter
        if tool in ['OpenZL', 'Genozip', 'SPring']:
            for x, y in zip(threads, times):
                if y is not None and y > 0:
                    # Push OpenZL text UP, Genozip DOWN, and SPring further UP to avoid overlap
                    if tool == 'OpenZL': offset = 10 
                    elif tool == 'Genozip': offset = -22
                    else: offset = 22 
                    
                    label = f'{y:.1f}s'
                    axs[2].annotate(label, (x, y), textcoords="offset points", xytext=(0, offset), 
                                    ha='center', fontsize=8, alpha=0.8, color=colors[tool])

# --- Formatting Panel C ---
axs[2].set_title('Multi-Core Scalability (Compression Time)', fontsize=14, fontweight='bold')
axs[2].set_xlabel('Number of CPU Threads', fontsize=12)
axs[2].set_ylabel('Time (Seconds)', fontsize=12)
axs[2].set_xticks(threads) # Forces the X-axis to only show 1, 2, 4, 8, 16
axs[2].grid(True, which='both', linestyle='--', alpha=0.5)
axs[2].legend(fontsize=10)

# ==========================================
# 5. SAVE ALL PLOTS (PDF & PNG)
# ==========================================
os.makedirs('plots', exist_ok=True)

# 1. Combined Figure
plt.savefig('plots/VCF_benchmark_combined.pdf', format='pdf', bbox_inches='tight', dpi=300)
plt.savefig('plots/VCF_benchmark_combined.png', format='png', bbox_inches='tight', dpi=300)

# 2. Panel A
extent_a = axs[0].get_window_extent().transformed(fig.dpi_scale_trans.inverted())
fig.savefig('plots/panel_a_ratios_VCF.pdf', bbox_inches=extent_a.expanded(1.2, 1.2))
fig.savefig('plots/panel_a_ratios_VCF.png', bbox_inches=extent_a.expanded(1.2, 1.2))

# 3. Panel B
extent_b = axs[1].get_window_extent().transformed(fig.dpi_scale_trans.inverted())
fig.savefig('plots/panel_b_rates_VCF.pdf', bbox_inches=extent_b.expanded(1.2, 1.2))
fig.savefig('plots/panel_b_rates_VCF.png', bbox_inches=extent_b.expanded(1.2, 1.2))

# 4. Panel C
extent_c = axs[2].get_window_extent().transformed(fig.dpi_scale_trans.inverted())
fig.savefig('plots/panel_c_scalability_VCF.pdf', bbox_inches=extent_c.expanded(1.2, 1.2))
fig.savefig('plots/panel_c_scalability_VCF.png', bbox_inches=extent_c.expanded(1.2, 1.2))

print("Successfully saved combined figures AND individual plots (PDF + PNG) to plots/ directory!")
