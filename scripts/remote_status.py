import sqlite3
import os
import subprocess

db_file = "/home/ubuntu/work/infomed_scraper/medicamentos.db"
log_file = "/home/ubuntu/work/infomed_scraper/pipeline.log"

print("="*60)
print("             INFOMED PIPELINE REMOTE STATUS")
print("="*60)

# Check process
ps_out = subprocess.getoutput("ps aux | grep 'python -m infomed.main' | grep -v grep")
if ps_out.strip():
    pid = ps_out.split()[1]
    cpu = ps_out.split()[2]
    mem = ps_out.split()[3]
    time_run = ps_out.split()[9]
    print(f"Process Status             : RUNNING (PID: {pid}, CPU: {cpu}%, MEM: {mem}%, Time: {time_run})")
else:
    print("Process Status             : COMPLETED / STOPPED")

if os.path.exists(db_file):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    
    total_records = c.execute("SELECT COUNT(*) FROM medicamentos").fetchone()[0]
    distinct_as = c.execute("SELECT COUNT(DISTINCT active_substance) FROM medicamentos WHERE active_substance IS NOT NULL AND LENGTH(TRIM(active_substance)) > 0").fetchone()[0]
    distinct_names = c.execute("SELECT COUNT(DISTINCT drug_name) FROM medicamentos WHERE drug_name IS NOT NULL").fetchone()[0]
    rcm_count = c.execute("SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 1").fetchone()[0]
    fi_count = c.execute("SELECT COUNT(*) FROM medicamentos WHERE fi_downloaded = 1").fetchone()[0]
    
    cft_done = c.execute("SELECT COUNT(*) FROM cft_progress").fetchone()[0] if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cft_progress'").fetchone() else 0
    atc_done = c.execute("SELECT COUNT(*) FROM atc_progress").fetchone()[0] if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='atc_progress'").fetchone() else 0
    
    print(f"Total Presentations in DB  : {total_records:,}")
    print(f"Distinct Active Substances : {distinct_as:,}")
    print(f"Distinct Drug Names        : {distinct_names:,}")
    print(f"Downloaded SmPCs / RCMs    : {rcm_count:,}")
    print(f"Downloaded Package Leaflets: {fi_count:,}")
    print(f"CFT Sweep Progress (Dim 1) : {cft_done} / 380 categories ({cft_done/380*100:.1f}%)")
    print(f"ATC Sweep Progress (Dim 2) : {atc_done} / 3,193 categories ({atc_done/3193*100:.1f}%)")
    
    # Sweep metrics
    if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sweep_metrics'").fetchone():
        metrics = c.execute("SELECT sweep_name, categories_processed, total_categories, medicines_encountered, rcms_downloaded, leaflets_downloaded, runtime_seconds FROM sweep_metrics").fetchall()
        if metrics:
            print("-"*60)
            print("Completed Sweeps:")
            for m in metrics:
                print(f"  • {m[0]}: {m[1]}/{m[2]} categories, {m[3]:,} medicines, {m[4]:,} RCMs, {m[5]:,} Leaflets in {m[6]/60:.1f}m")
    
    conn.close()
else:
    print("Database file medicamentos.db not found.")

print("-"*60)
print("Recent Pipeline Logs:")
if os.path.exists(log_file):
    tail_out = subprocess.getoutput(f"tail -n 10 {log_file}")
    for line in tail_out.splitlines():
        print(f"  {line}")
print("="*60)
