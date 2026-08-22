import sqlite3
import subprocess
import json

# Local DB stats
conn = sqlite3.connect("medicamentos.db")
c = conn.cursor()
local_total = c.execute("SELECT COUNT(*) FROM medicamentos").fetchone()[0]
local_as = c.execute("SELECT COUNT(DISTINCT active_substance) FROM medicamentos WHERE active_substance IS NOT NULL AND length(trim(active_substance)) > 0").fetchone()[0]
local_names = c.execute("SELECT COUNT(DISTINCT drug_name) FROM medicamentos WHERE drug_name IS NOT NULL").fetchone()[0]
local_rcms = c.execute("SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 1").fetchone()[0]
local_fis = c.execute("SELECT COUNT(*) FROM medicamentos WHERE fi_downloaded = 1").fetchone()[0]
local_both = c.execute("SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 1 AND fi_downloaded = 1").fetchone()[0]
local_neither = c.execute("SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 0 AND fi_downloaded = 0").fetchone()[0]

local_cft_done = c.execute("SELECT COUNT(*) FROM cft_progress").fetchone()[0] if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cft_progress'").fetchone() else 0
local_atc_done = c.execute("SELECT COUNT(*) FROM atc_progress").fetchone()[0] if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='atc_progress'").fetchone() else 0
conn.close()

# Run query on msc
remote_cmd = """python3 -c "
import sqlite3, json
conn = sqlite3.connect('/home/ubuntu/work/infomed_scraper/medicamentos.db')
c = conn.cursor()
r = {
    'total': c.execute('SELECT COUNT(*) FROM medicamentos').fetchone()[0],
    'as_count': c.execute('SELECT COUNT(DISTINCT active_substance) FROM medicamentos WHERE active_substance IS NOT NULL AND length(trim(active_substance)) > 0').fetchone()[0],
    'names': c.execute('SELECT COUNT(DISTINCT drug_name) FROM medicamentos WHERE drug_name IS NOT NULL').fetchone()[0],
    'rcms': c.execute('SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 1').fetchone()[0],
    'fis': c.execute('SELECT COUNT(*) FROM medicamentos WHERE fi_downloaded = 1').fetchone()[0],
    'both': c.execute('SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 1 AND fi_downloaded = 1').fetchone()[0],
    'neither': c.execute('SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 0 AND fi_downloaded = 0').fetchone()[0],
    'cft_done': c.execute('SELECT COUNT(*) FROM cft_progress').fetchone()[0] if c.execute(\\\"SELECT name FROM sqlite_master WHERE type='table' AND name='cft_progress'\\\").fetchone() else 0,
    'atc_done': c.execute('SELECT COUNT(*) FROM atc_progress').fetchone()[0] if c.execute(\\\"SELECT name FROM sqlite_master WHERE type='table' AND name='atc_progress'\\\").fetchone() else 0,
}
print(json.dumps(r))
"
"""

res = subprocess.run(["ssh", "msc", remote_cmd], capture_output=True, text=True)
remote = json.loads(res.stdout.strip())

print("="*90)
print(f"{'METRIC':<30} {'LOCAL RUN':<20} {'REMOTE (MSC) RUN':<20} {'DIFFERENCE'}")
print("="*90)
print(f"{'Total Presentations (Rows)':<30} {local_total:<20,} {remote['total']:<20,} {local_total - remote['total']:+,}")
print(f"{'Distinct Active Substances':<30} {local_as:<20,} {remote['as_count']:<20,} {local_as - remote['as_count']:+,}")
print(f"{'Distinct Drug / Brand Names':<30} {local_names:<20,} {remote['names']:<20,} {local_names - remote['names']:+,}")
print(f"{'Downloaded SmPCs (RCMs)':<30} {local_rcms:<20,} {remote['rcms']:<20,} {local_rcms - remote['rcms']:+,}")
print(f"{'Downloaded Leaflets (FIs)':<30} {local_fis:<20,} {remote['fis']:<20,} {local_fis - remote['fis']:+,}")
print(f"{'Both RCM + FI Downloaded':<30} {local_both:<20,} {remote['both']:<20,} {local_both - remote['both']:+,}")
print(f"{'Neither Downloaded (Missing)':<30} {local_neither:<20,} {remote['neither']:<20,} {local_neither - remote['neither']:+,}")
print(f"{'CFT Categories Processed':<30} {str(local_cft_done) + ' / 380':<20} {str(remote['cft_done']) + ' / 380':<20} -")
print(f"{'ATC Categories Processed':<30} {str(local_atc_done) + ' / 3,193':<20} {str(remote['atc_done']) + ' / 3,193':<20} -")
print("="*90)
