import os
import sys
import subprocess
import traceback

# List your Python scripts with their path relative to orchestrator.py
scripts = [
    "py/7_eleven_scraper.py",
    "py/makro_scraper.py",
    "py/big_c_scraper.py",
    "py/lotus_scraper.py",
    "py/tops_scraper.py",
]

print("🚀 Starting Sequential Scraper Pipeline...")
failed_jobs = []

for script_path in scripts:
    print("\n" + "="*50)
    print(f"▶️ RUNNING: {script_path}")
    print("="*50)
    
    try:
        # Run the python script using subprocess
        # sys.executable ensures it uses the same Python environment/venv
        subprocess.run(
            [sys.executable, script_path], 
            check=True
        )
        print(f"✅ SUCCESS: {script_path} completed cleanly.")
        
    except subprocess.CalledProcessError as e:
        # This catches errors if the script itself fails/crashes
        print(f"❌ ERROR: {script_path} failed with exit code {e.returncode}!")
        failed_jobs.append({"script": script_path, "error": f"Exit code {e.returncode}"})
        print("⏭️ Skipping to next script...")
        
    except Exception as e:
        # This catches unexpected system errors (e.g., file not found)
        print(f"❌ ERROR: {script_path} encountered an unexpected system error!")
        print("\n--- ERROR DETAILS ---")
        traceback.print_exc()
        print("----------------------\n")
        
        failed_jobs.append({"script": script_path, "error": str(e)})
        print("⏭️ Skipping to next script...")

# Pipeline Summary Reporting
print("\n" + "="*50)
print("🏁 PIPELINE RUN COMPLETE SUMMARY")
print("="*50)
if failed_jobs:
    print(f"⚠️ Done, but {len(failed_jobs)} job(s) failed:")
    for failure in failed_jobs:
        print(f" - {failure['script']} ({failure['error']})")
    
    # Exit with code 1 so GitHub Actions registers the run as a failure
    sys.exit(1) 
else:
    print("🎉 All scripts executed successfully with zero errors!")