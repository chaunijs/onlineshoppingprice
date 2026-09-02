import os
import sys
import subprocess
import traceback

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

scripts = [
    "py/scraper/7_eleven_scraper.py",
    "py/scraper/makro_scraper.py",
    "py/scraper/new_big_c_scraper.py",
    "py/scraper/lotus_scraper.py",
    "py/scraper/tops_scraper.py",
]

print("🚀 Starting Sequential Scraper Pipeline...")
failed_jobs = []

for script_path in scripts:
    print("\n" + "="*50)
    print(f"▶️ RUNNING: {script_path}")
    print("="*50)
    
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        result = subprocess.run(
            [sys.executable, script_path], 
            check=True,
            capture_output=False,  # Show stdout/stderr directly
            env=env
        )
        print(f"✅ SUCCESS: {script_path} completed cleanly.")
        
    except subprocess.CalledProcessError as e:
        print(f"❌ ERROR: {script_path} failed with exit code {e.returncode}!")
        failed_jobs.append({"script": script_path, "error": f"Exit code {e.returncode}"})
        print("⏭️ Skipping to next script...")
        
    except Exception as e:
        print(f"❌ ERROR: {script_path} encountered an unexpected system error!")
        print("\n--- ERROR DETAILS ---")
        traceback.print_exc()
        print("----------------------\n")
        
        failed_jobs.append({"script": script_path, "error": str(e)})
        print("⏭️ Skipping to next script...")

print("\n" + "="*50)
print("🏁 PIPELINE RUN COMPLETE SUMMARY")
print("="*50)
if failed_jobs:
    print(f"⚠️ Done, but {len(failed_jobs)} job(s) failed:")
    for failure in failed_jobs:
        print(f" - {failure['script']} ({failure['error']})")
    
    sys.exit(1)
else:
    print("🎉 All scripts executed successfully with zero errors!")
